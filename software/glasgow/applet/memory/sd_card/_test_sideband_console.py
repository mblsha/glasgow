import asyncio
import logging
import os
from pathlib import Path
import struct
import tempfile
import unittest

from ._protocol import BackendStatus, HostEvent, ReplyFrame, RequestFrame
from ._sideband_console import (
    BLOCK_SIZE, MAGIC, ConsoleFrame, ConsoleFrameFlag, ConsoleFrameKind,
    SidebandConsole, SidebandLocator, SidebandRegion,
)
from ._test_support import MemoryBackend, TestSDCardInterface


GENERATION = 0x4321
REQUEST_ID = 0x1234


class SidebandConsoleTestCase(unittest.TestCase):
    def make_console(self):
        return SidebandConsole(
            logging.getLogger(__name__),
            SidebandRegion(start_lba=8, sectors=8, image_sectors=16))

    def test_frame_round_trip_and_integrity(self):
        original = ConsoleFrame(
            ConsoleFrameKind.TargetOutput, 0x1122334455667788,
            sequence=9, acknowledgement=7, payload=b"hello\0world",
            flags=int(ConsoleFrameFlag.HostReady), status=3)
        block = original.encode()
        self.assertEqual(len(block), BLOCK_SIZE)
        self.assertEqual(block[:len(MAGIC)], MAGIC)
        self.assertEqual(ConsoleFrame.decode(block), original)

        damaged = bytearray(block)
        damaged[100] ^= 0x40
        with self.assertRaisesRegex(ValueError, "CRC"):
            ConsoleFrame.decode(damaged)

    def test_tail_locator_preserves_fourth_partition_and_boot_control_lbas(self):
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "image.img"
            image = bytearray(BLOCK_SIZE * 32)
            image[510:512] = b"\x55\xaa"
            image[BLOCK_SIZE:BLOCK_SIZE + 16] = b"boot-control-one"
            image[2 * BLOCK_SIZE:2 * BLOCK_SIZE + 16] = b"boot-control-two"
            entry = struct.pack(
                "<B3sB3sII", 0, b"\xfe\xff\xff", 0x83,
                b"\xfe\xff\xff", 8, 16)
            image[446 + 3 * 16:446 + 4 * 16] = entry
            locator = SidebandLocator(24, 8, 32)
            image[-BLOCK_SIZE:] = locator.encode()
            path.write_bytes(image)
            self.assertEqual(
                SidebandRegion.from_image(path),
                SidebandRegion(start_lba=24, sectors=8, image_sectors=32))
            self.assertEqual(
                path.read_bytes()[BLOCK_SIZE:BLOCK_SIZE + 16], b"boot-control-one")
            self.assertEqual(
                path.read_bytes()[2 * BLOCK_SIZE:2 * BLOCK_SIZE + 16],
                b"boot-control-two")

            struct.pack_into("<I", image, 446 + 3 * 16 + 12, 17)
            path.write_bytes(image)
            with self.assertRaisesRegex(ValueError, "overlaps MBR partition 4"):
                SidebandRegion.from_image(path)

    def test_tail_locator_crc_is_required(self):
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "image.img"
            image = bytearray(BLOCK_SIZE * 16)
            image[510:512] = b"\x55\xaa"
            image[-BLOCK_SIZE:] = SidebandLocator(8, 8, 16).encode()
            image[-100] ^= 0x40
            path.write_bytes(image)
            with self.assertRaisesRegex(ValueError, "CRC"):
                SidebandRegion.from_image(path)

    def test_target_output_retry_is_idempotent(self):
        console = self.make_console()
        address = console.region.start + BLOCK_SIZE
        frame = ConsoleFrame(
            ConsoleFrameKind.TargetOutput, console.session,
            sequence=1, payload=b"one\r\n").encode()
        console.write_block(address, frame)
        console.write_block(address, frame)
        self.assertEqual(console._target_output, b"one\r\n")

        changed = ConsoleFrame(
            ConsoleFrameKind.TargetOutput, console.session,
            sequence=1, payload=b"different").encode()
        with self.assertRaisesRegex(Exception, "different contents"):
            console.write_block(address, changed)

    def test_host_input_is_peeked_until_explicit_ack(self):
        console = self.make_console()
        console.queue_host_input(b"power-sweep stage\n")
        input_address = console.region.start + 2 * BLOCK_SIZE
        first = console.read_block(input_address)
        self.assertEqual(console.read_block(input_address), first)
        input_frame = ConsoleFrame.decode(first)
        self.assertEqual(input_frame.kind, ConsoleFrameKind.HostInput)
        self.assertEqual(input_frame.payload, b"power-sweep stage\n")

        ack = ConsoleFrame(
            ConsoleFrameKind.TargetAck, console.session,
            acknowledgement=input_frame.sequence).encode()
        ack_address = console.region.start + 3 * BLOCK_SIZE
        console.write_block(ack_address, ack)
        console.write_block(ack_address, ack)
        self.assertEqual(
            ConsoleFrame.decode(console.read_block(input_address)).kind,
            ConsoleFrameKind.Empty)

    def test_final_sector_replays_locator(self):
        console = self.make_console()
        locator_address = (
            console.region.start + (console.region.sectors - 1) * BLOCK_SIZE
        )
        self.assertEqual(
            SidebandLocator.decode(console.read_block(locator_address)),
            console.region.locator,
        )

    def test_interface_intercepts_without_touching_image_or_flush(self):
        console = self.make_console()
        backend = MemoryBackend(bytes(BLOCK_SIZE * 16))
        output = ConsoleFrame(
            ConsoleFrameKind.TargetOutput, console.session,
            sequence=1, payload=b"sideband").encode()
        request = RequestFrame.create(
            HostEvent.Write, GENERATION, REQUEST_ID,
            struct.pack("<I", console.region.start + BLOCK_SIZE) + output).encode_packet()
        iface = TestSDCardInterface(backend, request)
        iface._sideband_console = console

        asyncio.run(iface.serve_once())

        reply = ReplyFrame.decode_packet(bytes(iface._pipe.replies))
        self.assertEqual(reply.header.status, BackendStatus.OK)
        self.assertEqual(backend.image, bytes(BLOCK_SIZE * 16))
        self.assertEqual(backend.flushes, 0)
        self.assertEqual(console._target_output, b"sideband")

    def test_interface_rejects_erase_across_console_tail(self):
        console = self.make_console()
        backend = MemoryBackend(bytes([0x55]) * BLOCK_SIZE * 16)
        request = RequestFrame.create(
            HostEvent.Erase, GENERATION, REQUEST_ID,
            struct.pack("<II", console.region.start - BLOCK_SIZE,
                        console.region.start)).encode_packet()
        iface = TestSDCardInterface(backend, request)
        iface._sideband_console = console

        asyncio.run(iface.serve_once())

        reply = ReplyFrame.decode_packet(bytes(iface._pipe.replies))
        self.assertEqual(reply.header.status, BackendStatus.Invalid)
        self.assertEqual(backend.image, bytes([0x55]) * BLOCK_SIZE * 16)

    def test_persistent_pty_moves_bytes_in_both_directions(self):
        async def scenario():
            console = self.make_console()
            with tempfile.TemporaryDirectory() as temp_name:
                link = Path(temp_name) / "console"
                console.open_pty(link)
                client = os.open(link, os.O_RDWR | os.O_NONBLOCK | os.O_NOCTTY)
                task = asyncio.create_task(console.serve_pty())
                try:
                    os.write(client, b"host-command\n")
                    for _ in range(100):
                        await asyncio.sleep(0.001)
                        frame = ConsoleFrame.decode(console.read_block(
                            console.region.start + 2 * BLOCK_SIZE))
                        if frame.kind == ConsoleFrameKind.HostInput:
                            break
                    self.assertEqual(frame.payload, b"host-command\n")

                    output = ConsoleFrame(
                        ConsoleFrameKind.TargetOutput, console.session,
                        sequence=1, payload=b"target-output\r\n").encode()
                    console.write_block(
                        console.region.start + BLOCK_SIZE, output)
                    received = b""
                    for _ in range(100):
                        await asyncio.sleep(0.001)
                        try:
                            received += os.read(client, 4096)
                        except BlockingIOError:
                            pass
                        if b"target-output\r\n" in received:
                            break
                    self.assertIn(b"target-output\r\n", received)
                finally:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    os.close(client)
                    console.close()
                self.assertFalse(os.path.lexists(link))

        asyncio.run(scenario())

    def test_pty_client_can_disconnect_and_reconnect_without_output_loss(self):
        async def scenario():
            console = self.make_console()
            with tempfile.TemporaryDirectory() as temp_name:
                link = Path(temp_name) / "console"
                console.open_pty(link)
                first_client = os.open(
                    link, os.O_RDWR | os.O_NONBLOCK | os.O_NOCTTY)
                task = asyncio.create_task(console.serve_pty())
                try:
                    os.close(first_client)
                    output = ConsoleFrame(
                        ConsoleFrameKind.TargetOutput, console.session,
                        sequence=1, payload=b"survived-detach\r\n").encode()
                    console.write_block(
                        console.region.start + BLOCK_SIZE, output)
                    for _ in range(100):
                        await asyncio.sleep(0.001)
                        if not console._target_output:
                            break
                    self.assertFalse(console._target_output)

                    second_client = os.open(
                        link, os.O_RDWR | os.O_NONBLOCK | os.O_NOCTTY)
                    try:
                        received = b""
                        for _ in range(100):
                            await asyncio.sleep(0.001)
                            try:
                                received += os.read(second_client, 4096)
                            except BlockingIOError:
                                pass
                            if b"survived-detach\r\n" in received:
                                break
                        self.assertEqual(received.count(b"survived-detach\r\n"), 1)
                    finally:
                        os.close(second_client)
                finally:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    console.close()

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
