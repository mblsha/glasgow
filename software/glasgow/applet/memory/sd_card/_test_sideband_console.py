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
    SidebandConsole, SidebandPartition,
)
from ._test_support import MemoryBackend, TestSDCardInterface


GENERATION = 0x4321
REQUEST_ID = 0x1234


class SidebandConsoleTestCase(unittest.TestCase):
    def make_console(self):
        return SidebandConsole(
            logging.getLogger(__name__), SidebandPartition(start_lba=8, sectors=8))

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

    def test_partition_contract_requires_explicit_tail_partition(self):
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "image.img"
            image = bytearray(BLOCK_SIZE * 16)
            image[510:512] = b"\x55\xaa"
            entry = struct.pack(
                "<B3sB3sII", 0, b"\xfe\xff\xff", 0xda,
                b"\xfe\xff\xff", 8, 8)
            image[446 + 3 * 16:446 + 4 * 16] = entry
            path.write_bytes(image)
            self.assertEqual(
                SidebandPartition.from_image(path),
                SidebandPartition(start_lba=8, sectors=8))

            image[446 + 3 * 16 + 4] = 0x83
            path.write_bytes(image)
            with self.assertRaisesRegex(ValueError, "type-0xDA"):
                SidebandPartition.from_image(path)

    def test_target_output_retry_is_idempotent(self):
        console = self.make_console()
        address = console.partition.start + BLOCK_SIZE
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
        input_address = console.partition.start + 2 * BLOCK_SIZE
        first = console.read_block(input_address)
        self.assertEqual(console.read_block(input_address), first)
        input_frame = ConsoleFrame.decode(first)
        self.assertEqual(input_frame.kind, ConsoleFrameKind.HostInput)
        self.assertEqual(input_frame.payload, b"power-sweep stage\n")

        ack = ConsoleFrame(
            ConsoleFrameKind.TargetAck, console.session,
            acknowledgement=input_frame.sequence).encode()
        ack_address = console.partition.start + 3 * BLOCK_SIZE
        console.write_block(ack_address, ack)
        console.write_block(ack_address, ack)
        self.assertEqual(
            ConsoleFrame.decode(console.read_block(input_address)).kind,
            ConsoleFrameKind.Empty)

    def test_interface_intercepts_without_touching_image_or_flush(self):
        console = self.make_console()
        backend = MemoryBackend(bytes(BLOCK_SIZE * 16))
        output = ConsoleFrame(
            ConsoleFrameKind.TargetOutput, console.session,
            sequence=1, payload=b"sideband").encode()
        request = RequestFrame.create(
            HostEvent.Write, GENERATION, REQUEST_ID,
            struct.pack("<I", console.partition.start + BLOCK_SIZE) + output).encode_packet()
        iface = TestSDCardInterface(backend, request)
        iface._sideband_console = console

        asyncio.run(iface.serve_once())

        reply = ReplyFrame.decode_packet(bytes(iface._pipe.replies))
        self.assertEqual(reply.header.status, BackendStatus.OK)
        self.assertEqual(backend.image, bytes(BLOCK_SIZE * 16))
        self.assertEqual(backend.flushes, 0)
        self.assertEqual(console._target_output, b"sideband")

    def test_interface_rejects_erase_across_console_partition(self):
        console = self.make_console()
        backend = MemoryBackend(bytes([0x55]) * BLOCK_SIZE * 16)
        request = RequestFrame.create(
            HostEvent.Erase, GENERATION, REQUEST_ID,
            struct.pack("<II", console.partition.start - BLOCK_SIZE,
                        console.partition.start)).encode_packet()
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
                            console.partition.start + 2 * BLOCK_SIZE))
                        if frame.kind == ConsoleFrameKind.HostInput:
                            break
                    self.assertEqual(frame.payload, b"host-command\n")

                    output = ConsoleFrame(
                        ConsoleFrameKind.TargetOutput, console.session,
                        sequence=1, payload=b"target-output\r\n").encode()
                    console.write_block(
                        console.partition.start + BLOCK_SIZE, output)
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


if __name__ == "__main__":
    unittest.main()
