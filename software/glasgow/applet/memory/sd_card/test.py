import asyncio
from collections import deque
import os
import random
import struct
import tempfile
import time
import unittest

from amaranth import ClockDomain, Module
from amaranth.sim import Simulator

from glasgow.applet import GlasgowAppletV2TestCase, synthesis_test
from glasgow.gateware import cobs as gateware_cobs
from glasgow.simulation.assembly import SimulationAssembly

from . import (
    BLOCK_SIZE, BackendError, BackendStatus, BackendWorker, BlockBackend, FileBlockBackend,
    CSDTransferSpeedProfile, HostEvent, HostReply,
    ReplyFrame, RequestFrame, SDCardApplet, SDCardInterface,
    build_cid, build_csd, build_scr, crc7, crc16,
)
from .bfm import SDHostBFM
from ._console import SerialTransport, TelnetTransport
from .e2e_zaurus import ZaurusSDController
from ._buffer import BlockBuffer
from ._fat16 import FAT16Image, create_fat16_image
from ._protocol import (
    HOST_OPERATIONS, HOST_OPERATION_FOR_EVENT, HOST_OPERATION_FOR_REPLY,
    FrameError, frame_crc16,
)


GENERATION = 0x4321
REQUEST_ID = 0x1234


class SDCardAppletTestCase(GlasgowAppletV2TestCase, applet=SDCardApplet):
    @synthesis_test
    def test_build(self):
        self.assertBuilds(revision="C3")

    def test_crc_vectors(self):
        self.assertEqual(crc7(0x4000_0000_00, 40), 0x4a)  # CMD0
        self.assertEqual(crc7(0x5100_0000_00, 40), 0x2a)  # CMD17, address 0
        self.assertEqual(crc16(bytes([0xff]) * BLOCK_SIZE), 0x7fa1)

    def test_register_crcs(self):
        for register in (build_cid(), build_csd(16 * 1024 * 1024)):
            self.assertEqual((register >> 1) & 0x7f, crc7(register >> 8, 120))
            self.assertEqual(register & 1, 1)

    def test_read_only_csd(self):
        self.assertEqual((build_csd(16 * 1024 * 1024) >> 12) & 1, 0)
        self.assertEqual((build_csd(16 * 1024 * 1024, read_only=True) >> 12) & 1, 1)

    def test_csd_transfer_speed_profiles(self):
        default = build_csd(16 * 1024 * 1024)
        conservative = build_csd(
            16 * 1024 * 1024,
            transfer_speed=CSDTransferSpeedProfile.Conservative)
        self.assertEqual((default >> 96) & 0xff, CSDTransferSpeedProfile.Default)
        self.assertEqual((conservative >> 96) & 0xff,
            CSDTransferSpeedProfile.Conservative)
        self.assertEqual((conservative >> 1) & 0x7f, crc7(conservative >> 8, 120))

    def test_csd_capacity_through_16_mib(self):
        for size in (2048, 1 << 20, 8 << 20, 16 << 20):
            csd = build_csd(size)
            read_block_length = 1 << ((csd >> 80) & 0xf)
            c_size = (csd >> 62) & 0xfff
            c_size_mult = (csd >> 47) & 0x7
            capacity = (c_size + 1) * (1 << (c_size_mult + 2)) * read_block_length
            self.assertEqual(capacity, size)

    def test_scr_advertises_one_and_four_bit_modes(self):
        scr = build_scr()
        self.assertEqual((scr >> 56) & 0xf, 0)  # SD 1.0/1.01
        self.assertEqual((scr >> 48) & 0xf, 0b0101)

    async def _initialize_sim_card(self, applet, bfm, image, payload, *, transfer_speed):
        # Let the enable synchronizer cross into the externally clocked SD domain.
        await bfm.idle(4)

        # Configuration is snapshotted atomically before command sampling. Mutating the source
        # register while enabled must not tear or alter the active 128-bit CID.
        await applet.sd_card_iface._cid.set(0)
        await bfm.send_command(0)
        for _ in range(4):
            command_oe, _, _ = await bfm.cycle()
            self.assertEqual(command_oe, 0)

        await bfm.send_command(55)
        self.assertEqual(await applet.sd_card_iface.last_command, 55)
        bfm.assert_r1(await bfm.receive_response(48), 55)

        await bfm.send_command(41, 0x00ff_8000)
        r3 = await bfm.receive_response(48)
        self.assertEqual(r3 >> 46, 0)
        self.assertEqual((r3 >> 40) & 0x3f, 0x3f)
        self.assertEqual((r3 >> 8) & 0xffff_ffff, 0x80ff_8000)
        self.assertEqual(r3 & 0xff, 0xff)

        stat = os.fstat(image.fileno())
        cid = build_cid(serial=(stat.st_ino ^ stat.st_size) & 0xffff_ffff)
        csd = build_csd(len(payload), transfer_speed=transfer_speed)

        await bfm.send_command(2)
        r2 = await bfm.receive_response(136)
        self.assertEqual((r2 >> 1) & ((1 << 127) - 1), cid >> 1)

        await bfm.send_command(3)
        r6 = await bfm.receive_response(48)
        self.assertEqual((r6 >> 24) & 0xffff, 0x1234)
        self.assertEqual((r6 >> 40) & 0x3f, 3)

        await bfm.send_command(9, 0x1234_0000)
        r2 = await bfm.receive_response(136)
        self.assertEqual((r2 >> 1) & ((1 << 127) - 1), csd >> 1)

        await bfm.send_command(7, 0x1234_0000)
        bfm.assert_r1(await bfm.receive_response(48), 7)
        await bfm.send_command(16, BLOCK_SIZE)
        bfm.assert_r1(await bfm.receive_response(48), 16)

    async def _serve_next_sim_request(self, applet, bfm):
        while not applet.sd_card_iface._pipe.flush_readable:
            await bfm.cycle()
        await applet.sd_card_iface.serve_once()

    async def _receive_sim_block(self, applet, bfm, expected, *, timeout_cycles=2048):
        async def diagnostic():
            pipe = applet.sd_card_iface._pipe
            return (
                f"host readable={pipe.readable}, "
                f"errors={await applet.sd_card_iface.protocol_errors}, "
                f"prefetch waits={await applet.sd_card_iface.prefetch_waits}")

        await bfm.receive_data_block(
            expected, timeout_cycles=timeout_cycles, timeout_diagnostic=diagnostic)

    async def _complete_sim_busy_request(self, applet, bfm, description):
        saw_busy = False
        for _ in range(4096):
            _, _, data_sample = await bfm.cycle()
            if data_sample[0] == (1, 0):
                saw_busy = True
            if applet.sd_card_iface._pipe.flush_readable:
                await applet.sd_card_iface.serve_once()
            if saw_busy and data_sample[0][0] == 0:
                return
        pipe = applet.sd_card_iface._pipe
        self.fail(
            f"timed out waiting for {description} (host readable={pipe.readable}, "
            f"errors={await applet.sd_card_iface.protocol_errors})")

    def _run_simulation(self, payload, scenario, *, setup_arguments=()):
        with tempfile.NamedTemporaryFile() as image:
            image.write(payload)
            image.flush()

            args = self._parse_args([*setup_arguments, image.name])
            assembly = SimulationAssembly()
            applet = self.applet_cls(assembly)
            applet.build(args)
            applet.sd_card_iface._worker_factory = _InlineWorker

            clk = assembly.get_pin("A2")
            cmd = assembly.get_pin("A5")
            dat = [assembly.get_pin(name) for name in ("A1", "A0", "A7", "A6")]

            async def card_host(ctx):
                bfm = SDHostBFM(ctx, self, clk=clk, cmd=cmd, dat=dat)
                transfer_speed = (CSDTransferSpeedProfile.Conservative
                    if args.conservative_speed else CSDTransferSpeedProfile.Default)
                await self._initialize_sim_card(
                    applet, bfm, image, payload, transfer_speed=transfer_speed)
                try:
                    await scenario(applet, bfm, image)
                finally:
                    await applet.sd_card_iface.close()

            async def main(ctx):
                await applet.setup(args)
                await card_host(ctx)

            assembly.run(main)

    def test_sim_initialization_and_single_block_read(self):
        payload = bytes(
            ((index // BLOCK_SIZE) * 0x31 + index) & 0xff
            for index in range(BLOCK_SIZE * 8))

        async def scenario(applet, bfm, image):
            await bfm.send_command(17, BLOCK_SIZE)
            bfm.assert_r1(await bfm.receive_response(48), 17)
            await self._serve_next_sim_request(applet, bfm)
            await self._receive_sim_block(
                applet, bfm, payload[BLOCK_SIZE:BLOCK_SIZE * 2])

        self._run_simulation(payload, scenario)

    def test_sim_single_and_multi_bit_writes_and_erase(self):
        payload = bytes(
            ((index // BLOCK_SIZE) * 0x31 + index) & 0xff
            for index in range(BLOCK_SIZE * 8))

        async def scenario(applet, bfm, image):
            one_bit_address = BLOCK_SIZE * 2
            one_bit_block = bytes((0x5a ^ index) & 0xff for index in range(BLOCK_SIZE))
            await bfm.send_command(24, one_bit_address)
            bfm.assert_r1(await bfm.receive_response(48), 24)
            await bfm.send_data_block_1bit(one_bit_block)
            self.assertEqual(await bfm.receive_data_response(), [0, 0, 1, 0, 1])
            await self._complete_sim_busy_request(
                applet, bfm, "host-backed 1-bit write completion")
            self.assertEqual(
                os.pread(image.fileno(), BLOCK_SIZE, one_bit_address), one_bit_block)

            await bfm.send_command(55)
            bfm.assert_r1(await bfm.receive_response(48), 55)
            await bfm.send_command(6, 2)
            bfm.assert_r1(await bfm.receive_response(48), 6)

            write_address = BLOCK_SIZE * 3
            write_block = bytes((0xa5 ^ index) & 0xff for index in range(BLOCK_SIZE))
            await bfm.send_command(24, write_address)
            bfm.assert_r1(await bfm.receive_response(48), 24)
            await bfm.send_data_block_4bit(write_block)
            self.assertEqual(await bfm.receive_data_response(), [0, 0, 1, 0, 1])
            await self._complete_sim_busy_request(
                applet, bfm, "host-backed 4-bit write completion")
            self.assertEqual(os.pread(image.fileno(), BLOCK_SIZE, write_address), write_block)

            rejected_address = 0
            rejected_block = bytes([0x3c]) * BLOCK_SIZE
            original_block = os.pread(image.fileno(), BLOCK_SIZE, rejected_address)
            writes_before = await applet.sd_card_iface.write_count
            await bfm.send_command(24, rejected_address)
            bfm.assert_r1(await bfm.receive_response(48), 24)
            await bfm.send_data_block_4bit(rejected_block, valid_crc=False)
            self.assertEqual(await bfm.receive_data_response(), [0, 1, 0, 1, 1])
            await bfm.idle(32)
            self.assertFalse(applet.sd_card_iface._pipe.flush_readable)
            self.assertEqual(await applet.sd_card_iface.write_count, writes_before)
            self.assertEqual(
                os.pread(image.fileno(), BLOCK_SIZE, rejected_address), original_block)

            erase_address = BLOCK_SIZE
            await bfm.send_command(32, erase_address)
            bfm.assert_r1(await bfm.receive_response(48), 32)
            await bfm.send_command(33, erase_address)
            bfm.assert_r1(await bfm.receive_response(48), 33)
            await bfm.send_command(38)
            bfm.assert_r1(await bfm.receive_response(48), 38)
            await self._complete_sim_busy_request(
                applet, bfm, "host-backed erase completion")
            self.assertEqual(
                os.pread(image.fileno(), BLOCK_SIZE, erase_address),
                bytes([0xff]) * BLOCK_SIZE)

        self._run_simulation(payload, scenario)

    def test_sim_sustained_cmd18_with_host_stalls_and_random_cmd12(self):
        payload = b"".join(
            bytes(((block * 0x31) + index) & 0xff for index in range(BLOCK_SIZE))
            for block in range(48))

        async def scenario(applet, bfm, image):
            rng = random.Random(0x18_12_860)
            stalls = deque([0, 5000] + [rng.randrange(0, 5200) for _ in range(32)])
            stall_remaining = None

            async def service_host():
                nonlocal stall_remaining
                if not applet.sd_card_iface._pipe.flush_readable:
                    stall_remaining = None
                    return
                if stall_remaining is None:
                    stall_remaining = stalls.popleft() if stalls else 0
                if stall_remaining:
                    stall_remaining -= 1
                    return
                await applet.sd_card_iface.serve_once()
                stall_remaining = None

            bfm.cycle_hook = service_host
            for round_index in range(3):
                first_block = round_index * 12
                transferred_blocks = 4 + rng.randrange(3)
                await bfm.send_command(18, first_block * BLOCK_SIZE)
                bfm.assert_r1(await bfm.receive_response(48), 18)
                for block in range(first_block, first_block + transferred_blocks):
                    await self._receive_sim_block(
                        applet, bfm,
                        payload[block * BLOCK_SIZE:(block + 1) * BLOCK_SIZE],
                        timeout_cycles=8192)

                # Vary CMD12 from immediately after a complete block to well into the following
                # block. The pending prefetch may still be Filling, already Ready, or Active.
                await bfm.idle(rng.randrange(512))
                await bfm.send_command(12)
                bfm.assert_r1(await bfm.receive_response(48), 12)
                await bfm.idle(BLOCK_SIZE * 8 + 64)

                guard_block = 40 + round_index
                await bfm.send_command(17, guard_block * BLOCK_SIZE)
                bfm.assert_r1(await bfm.receive_response(48), 17)
                await self._receive_sim_block(
                    applet, bfm,
                    payload[guard_block * BLOCK_SIZE:(guard_block + 1) * BLOCK_SIZE],
                    timeout_cycles=8192)

            bfm.cycle_hook = None
            self.assertGreater(await applet.sd_card_iface.prefetch_waits, 0)
            self.assertGreater(await applet.sd_card_iface.max_host_service_latency, 0)
            self.assertEqual(await applet.sd_card_iface.buffer_faults, 0)

        self._run_simulation(payload, scenario)


class _FakePipe:
    def __init__(self, request):
        self.request = bytearray(request)
        self.replies = bytearray()
        self.flushes = 0

    async def recv(self, length):
        assert len(self.request) >= length
        data = self.request[:length]
        del self.request[:length]
        return memoryview(data)

    async def recv_until(self, delimiter):
        length = self.request.index(delimiter) + len(delimiter)
        data = self.request[:length]
        del self.request[:length]
        return bytes(data)

    async def send(self, data):
        self.replies.extend(data)

    async def flush(self):
        self.flushes += 1

    async def reset(self):
        self.request.clear()
        self.replies.clear()


class _MemoryBackend(BlockBackend):
    def __init__(self, image, *, read_only=False):
        self.image = bytearray(image)
        self._read_only = read_only
        self.flushes = 0
        self.closed = False

    @property
    def size(self):
        return len(self.image)

    @property
    def read_only(self):
        return self._read_only

    @property
    def identity(self):
        return 0x1234_5678

    def _check(self, address):
        if address % BLOCK_SIZE or address < 0 or address > self.size - BLOCK_SIZE:
            raise BackendError(BackendStatus.Invalid, "invalid block")

    def read_block(self, address):
        self._check(address)
        return bytes(self.image[address:address + BLOCK_SIZE])

    def write_block(self, address, block):
        self._check(address)
        if self.read_only:
            raise BackendError(BackendStatus.ReadOnly, "read-only backend")
        self.image[address:address + BLOCK_SIZE] = block

    def erase(self, start, end):
        self._check(start)
        self._check(end)
        if start > end:
            raise BackendError(BackendStatus.Invalid, "invalid erase range")
        if self.read_only:
            raise BackendError(BackendStatus.ReadOnly, "read-only backend")
        self.image[start:end + BLOCK_SIZE] = bytes([0xff]) * (end - start + BLOCK_SIZE)

    def flush(self):
        self.flushes += 1

    def close(self):
        self.closed = True


class _InlineWorker:
    def __init__(self, backend):
        self.backend = backend

    async def read_block(self, address):
        return self.backend.read_block(address)

    async def write_block(self, address, block):
        return self.backend.write_block(address, block)

    async def erase(self, start, end):
        return self.backend.erase(start, end)

    async def flush(self):
        return self.backend.flush()

    async def close(self):
        return self.backend.close()


class SDCardInterfaceTestCase(unittest.TestCase):
    def make_iface(self, backend, request, *, generation=GENERATION, sync_writes=True):
        iface = object.__new__(SDCardInterface)
        iface._backend = backend
        iface._worker = _InlineWorker(backend)
        iface._generation = generation
        iface._sync_writes = sync_writes
        iface._flush_count = 0
        iface._host_backend_errors = 0
        iface._pipe = _FakePipe(request)
        iface._logger = __import__("logging").getLogger(__name__)
        iface._level = __import__("logging").DEBUG
        return iface

    def request(self, opcode, payload=b"", *, generation=GENERATION, request_id=REQUEST_ID):
        return RequestFrame.create(opcode, generation, request_id, payload).encode_packet()

    def reply(self, pipe):
        reply = ReplyFrame.decode_packet(bytes(pipe.replies))
        return reply.header, reply.payload

    def test_host_read_event(self):
        image = bytes(index & 0xff for index in range(BLOCK_SIZE * 4))
        backend = _MemoryBackend(image)
        request = self.request(HostEvent.Read, struct.pack("<I", BLOCK_SIZE))
        iface = self.make_iface(backend, request)
        asyncio.run(iface.serve_once())
        header, payload = self.reply(iface._pipe)
        self.assertEqual(header.opcode, HostReply.ReadData)
        self.assertEqual(header.generation, GENERATION)
        self.assertEqual(header.request_id, REQUEST_ID)
        self.assertEqual(header.status, BackendStatus.OK)
        self.assertEqual(payload, image[BLOCK_SIZE:BLOCK_SIZE * 2])
        self.assertEqual(iface._pipe.flushes, 1)

    def test_host_read_error_is_reported(self):
        backend = _MemoryBackend(bytes(BLOCK_SIZE * 4))
        request = self.request(HostEvent.Read, struct.pack("<I", 1))
        iface = self.make_iface(backend, request)
        asyncio.run(iface.serve_once())
        header, payload = self.reply(iface._pipe)
        self.assertEqual(header.opcode, HostReply.ReadData)
        self.assertEqual(header.status, BackendStatus.Invalid)
        self.assertEqual(payload, bytes([0xff]) * BLOCK_SIZE)
        self.assertEqual(iface._host_backend_errors, 1)

    def test_host_write_event(self):
        image_size = BLOCK_SIZE * 4
        payload = bytes([0xa5]) * BLOCK_SIZE
        backend = _MemoryBackend(bytes(image_size))
        request = self.request(
            HostEvent.Write, struct.pack("<I", BLOCK_SIZE) + payload)
        iface = self.make_iface(backend, request)
        asyncio.run(iface.serve_once())
        header, reply_payload = self.reply(iface._pipe)
        self.assertEqual(header.opcode, HostReply.WriteAck)
        self.assertEqual(header.status, BackendStatus.OK)
        self.assertEqual(reply_payload, b"")
        self.assertEqual(backend.image[BLOCK_SIZE:BLOCK_SIZE * 2], payload)
        self.assertEqual(backend.flushes, 1)

    def test_volatile_write_skips_per_request_flush(self):
        backend = _MemoryBackend(bytes(BLOCK_SIZE * 4))
        payload = bytes([0xa5]) * BLOCK_SIZE
        request = self.request(
            HostEvent.Write, struct.pack("<I", BLOCK_SIZE) + payload)
        iface = self.make_iface(backend, request, sync_writes=False)
        asyncio.run(iface.serve_once())
        header, _ = self.reply(iface._pipe)
        self.assertEqual(header.status, BackendStatus.OK)
        self.assertEqual(backend.flushes, 0)

    def test_host_erase_event(self):
        image_size = BLOCK_SIZE * 4
        backend = _MemoryBackend(bytes([0x55]) * image_size)
        request = self.request(HostEvent.Erase, struct.pack(
            "<II", BLOCK_SIZE, BLOCK_SIZE * 2))
        iface = self.make_iface(backend, request)
        asyncio.run(iface.serve_once())
        header, payload = self.reply(iface._pipe)
        self.assertEqual(header.opcode, HostReply.EraseAck)
        self.assertEqual(header.status, BackendStatus.OK)
        self.assertEqual(payload, b"")
        self.assertEqual(
            backend.image[BLOCK_SIZE:BLOCK_SIZE * 3], bytes([0xff]) * BLOCK_SIZE * 2)
        self.assertEqual(backend.flushes, 1)

    def test_stale_generation_is_discarded(self):
        backend = _MemoryBackend(bytes(BLOCK_SIZE * 4))
        payload = bytes([0xa5]) * BLOCK_SIZE
        request = self.request(
            HostEvent.Write, struct.pack("<I", BLOCK_SIZE) + payload,
            generation=GENERATION ^ 0xffff)
        iface = self.make_iface(backend, request)
        asyncio.run(iface.serve_once())
        self.assertEqual(iface._pipe.replies, b"")
        self.assertEqual(backend.image, bytes(BLOCK_SIZE * 4))

    def test_read_only_backend_reports_status(self):
        backend = _MemoryBackend(bytes(BLOCK_SIZE * 4), read_only=True)
        payload = bytes([0xa5]) * BLOCK_SIZE
        request = self.request(
            HostEvent.Write, struct.pack("<I", BLOCK_SIZE) + payload)
        iface = self.make_iface(backend, request)
        asyncio.run(iface.serve_once())
        header, reply_payload = self.reply(iface._pipe)
        self.assertEqual(header.opcode, HostReply.WriteAck)
        self.assertEqual(header.status, BackendStatus.ReadOnly)
        self.assertEqual(reply_payload, b"")
        self.assertEqual(iface._host_backend_errors, 1)

    def test_malformed_frame_is_discarded_before_next_request(self):
        backend = _MemoryBackend(bytes(BLOCK_SIZE * 4))
        malformed = bytearray(self.request(HostEvent.Read, struct.pack("<I", 0)))
        malformed[2] ^= 0x40
        valid = self.request(HostEvent.Read, struct.pack("<I", BLOCK_SIZE))
        iface = self.make_iface(backend, bytes(malformed) + valid)
        asyncio.run(iface.serve_once())
        header, payload = self.reply(iface._pipe)
        self.assertEqual(header.status, BackendStatus.OK)
        self.assertEqual(payload, bytes(BLOCK_SIZE))

    def test_semantically_invalid_request_length_is_rejected(self):
        backend = _MemoryBackend(bytes(BLOCK_SIZE * 4))
        request = self.request(HostEvent.Write, b"too short")
        iface = self.make_iface(backend, request)
        asyncio.run(iface.serve_once())
        header, payload = self.reply(iface._pipe)
        self.assertEqual(header.opcode, HostReply.WriteAck)
        self.assertEqual(header.status, BackendStatus.Invalid)
        self.assertEqual(payload, b"")

    def test_power_monitor_suspends_and_resumes(self):
        class Device:
            def __init__(self):
                self.samples = iter((3.3, 0.0, 0.0, 3.3, 3.3))
                self.last = 3.3

            async def measure_voltage(self, port):
                self.last = next(self.samples, self.last)
                return self.last

        async def scenario():
            iface = object.__new__(SDCardInterface)
            iface._logger = __import__("logging").getLogger(__name__)
            iface._active = True
            transitions = []
            complete = asyncio.Event()

            async def flush():
                transitions.append("flush")

            async def suspend(*, reason):
                transitions.append("suspend")
                iface._active = False

            async def resume(*, reason):
                transitions.append("resume")
                iface._active = True
                complete.set()

            iface.flush = flush
            iface.suspend = suspend
            iface.resume = resume
            task = asyncio.create_task(iface.monitor_power(
                Device(), "B", interval=0.001, debounce_samples=2))
            await asyncio.wait_for(complete.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(transitions, ["suspend", "flush", "resume"])

        asyncio.run(scenario())


class SDCardProtocolTestCase(unittest.TestCase):
    def test_operation_schema_is_the_payload_source_of_truth(self):
        self.assertEqual(
            set(HOST_OPERATION_FOR_EVENT.values()), set(HOST_OPERATIONS))
        self.assertEqual(
            set(HOST_OPERATION_FOR_REPLY.values()), set(HOST_OPERATIONS))

        read = HOST_OPERATION_FOR_EVENT[HostEvent.Read]
        self.assertEqual(read.reply, HostReply.ReadData)
        encoded = read.request_payload.encode(address=0x1234_5600)
        self.assertEqual(read.request_payload.decode(encoded), {"address": 0x1234_5600})

        write = HOST_OPERATION_FOR_EVENT[HostEvent.Write]
        block = bytes(range(256)) * 2
        encoded = write.request_payload.encode(address=BLOCK_SIZE, data=block)
        self.assertEqual(write.request_payload.decode(encoded), {
            "address": BLOCK_SIZE,
            "data": block,
        })
        self.assertEqual(write.reply_payload.size, 0)

        erase = HOST_OPERATION_FOR_EVENT[HostEvent.Erase]
        encoded = erase.request_payload.encode(start=BLOCK_SIZE, end=BLOCK_SIZE * 3)
        self.assertEqual(erase.request_payload.decode(encoded), {
            "start": BLOCK_SIZE,
            "end": BLOCK_SIZE * 3,
        })

    def test_request_and_reply_frames_round_trip(self):
        request = RequestFrame.create(
            HostEvent.Write, GENERATION, REQUEST_ID,
            struct.pack("<I", BLOCK_SIZE) + bytes([0xa5]) * BLOCK_SIZE)
        self.assertEqual(RequestFrame.decode_packet(request.encode_packet()).payload,
            request.payload)

        reply = ReplyFrame.create(
            HostReply.ReadData, GENERATION, REQUEST_ID,
            BackendStatus.OK, bytes(range(256)) * 2)
        decoded = ReplyFrame.decode_packet(reply.encode_packet())
        self.assertEqual(decoded.header.generation, GENERATION)
        self.assertEqual(decoded.payload, reply.payload)

    def test_frame_crc_rejects_corruption(self):
        packet = bytearray(RequestFrame.create(
            HostEvent.Read, GENERATION, REQUEST_ID, struct.pack("<I", 0)).encode_packet())
        packet[3] ^= 1
        with self.assertRaises(FrameError):
            RequestFrame.decode_packet(packet)
        self.assertEqual(frame_crc16(b"123456789"), 0x31c3)

    def test_unknown_opcode_is_a_frame_error(self):
        raw = struct.pack("<BBHHH", 1, 0x7f, GENERATION, REQUEST_ID, 0)
        packet = gateware_cobs.encode(raw + struct.pack("<H", frame_crc16(raw))) + b"\0"
        with self.assertRaises(FrameError):
            RequestFrame.decode_packet(packet)


class BlockBufferTestCase(unittest.TestCase):
    def test_collisions_are_counted_once_per_incident(self):
        m = Module()
        m.domains.sd = ClockDomain()
        m.submodules.dut = dut = BlockBuffer()

        async def testbench(ctx):
            ctx.set(dut.host.write_en, 1)
            ctx.set(dut.data.write_en, 1)
            self.assertEqual(ctx.get(dut.collision), 1)
            await ctx.tick("sd")
            self.assertEqual(ctx.get(dut.collision_count), 1)
            self.assertEqual(ctx.get(dut.fault), 1)
            await ctx.tick("sd").repeat(3)
            self.assertEqual(ctx.get(dut.collision_count), 1)

            ctx.set(dut.host.write_en, 0)
            ctx.set(dut.data.write_en, 0)
            await ctx.tick("sd")
            ctx.set(dut.host.read_active, 1)
            ctx.set(dut.data.read_active, 1)
            await ctx.tick("sd")
            self.assertEqual(ctx.get(dut.collision_count), 2)
            self.assertEqual(ctx.get(dut.fault), 1)

            ctx.set(dut.host.read_active, 0)
            ctx.set(dut.data.read_active, 0)
            await ctx.tick("sd")

            ctx.set(dut.data.read_active, 1)
            ctx.set(dut.host.write_en, 1)
            ctx.set(dut.host.write_address, BLOCK_SIZE)
            ctx.set(dut.data.read_address, BLOCK_SIZE + 7)
            await ctx.tick("sd")
            self.assertEqual(ctx.get(dut.collision_count), 3)

        simulator = Simulator(m)
        simulator.add_clock(1e-6, domain="sd")
        simulator.add_testbench(testbench)
        simulator.run()


class FAT16ImageTestCase(unittest.TestCase):
    def test_generator_is_deterministic_and_structurally_readable(self):
        files = {
            "HOST.TXT": b"glasgow-host-marker\n",
            "BLOCK.BIN": bytes(range(256)) * 7,
        }
        with tempfile.TemporaryDirectory() as directory:
            first = os.path.join(directory, "first.img")
            second = os.path.join(directory, "second.img")
            create_fat16_image(first, files)
            create_fat16_image(second, files)
            with open(first, "rb") as first_file, open(second, "rb") as second_file:
                self.assertEqual(first_file.read(), second_file.read())
            image = FAT16Image.from_path(first)
            self.assertEqual(image.read_file("host.txt"), files["HOST.TXT"])
            self.assertEqual(image.read_file("BLOCK.BIN"), files["BLOCK.BIN"])


class BackendWorkerTestCase(unittest.TestCase):
    def test_read_ahead_is_selected_by_backend_capability(self):
        class CountingBackend(_MemoryBackend):
            def __init__(self, image, *, preferred=1):
                super().__init__(image)
                self.preferred = preferred
                self.runs = []

            @property
            def preferred_read_ahead_blocks(self):
                return self.preferred

            def read_blocks(self, address, count):
                self.runs.append((address, count))
                return super().read_blocks(address, count)

        async def scenario():
            image = bytes(BLOCK_SIZE * 8)
            generic = CountingBackend(image)
            generic_worker = BackendWorker(generic)
            self.assertEqual(generic_worker.read_ahead_blocks, 1)
            await generic_worker.read_block(0)
            await generic_worker.read_block(BLOCK_SIZE)
            self.assertEqual(generic.runs, [(0, 1), (BLOCK_SIZE, 1)])
            await generic_worker.close()

            optimized = CountingBackend(image, preferred=4)
            optimized_worker = BackendWorker(optimized)
            self.assertEqual(optimized_worker.read_ahead_blocks, 4)
            await optimized_worker.read_block(0)
            await optimized_worker.read_block(BLOCK_SIZE)
            self.assertEqual(optimized.runs, [(0, 4)])
            await optimized_worker.close()

            with tempfile.NamedTemporaryFile() as image_file:
                image_file.truncate(BLOCK_SIZE * 8)
                file_backend = FileBlockBackend(image_file.name)
                file_worker = BackendWorker(file_backend)
                self.assertEqual(file_worker.read_ahead_blocks, 256)
                await file_worker.close()

        asyncio.run(scenario())

    def test_sequential_reads_use_bounded_read_ahead_and_writes_invalidate_it(self):
        class CountingBackend(_MemoryBackend):
            def __init__(self, image):
                super().__init__(image)
                self.runs = []

            def read_blocks(self, address, count):
                self.runs.append((address, count))
                return super().read_blocks(address, count)

        async def scenario():
            image = b"".join(bytes([index]) * BLOCK_SIZE for index in range(8))
            backend = CountingBackend(image)
            worker = BackendWorker(backend, read_ahead_blocks=4)
            self.assertEqual(await worker.read_block(0), bytes([0]) * BLOCK_SIZE)
            self.assertEqual(await worker.read_block(BLOCK_SIZE), bytes([1]) * BLOCK_SIZE)
            self.assertEqual(backend.runs, [(0, 4)])

            replacement = bytes([0xa5]) * BLOCK_SIZE
            await worker.write_block(BLOCK_SIZE, replacement)
            self.assertEqual(await worker.read_block(BLOCK_SIZE), replacement)
            self.assertEqual(backend.runs, [(0, 4), (BLOCK_SIZE, 4)])
            await worker.close()

        asyncio.run(scenario())

    def test_blocking_backend_does_not_stall_event_loop(self):
        class SlowBackend(_MemoryBackend):
            def read_block(self, address):
                time.sleep(0.05)
                return super().read_block(address)

        async def scenario():
            backend = SlowBackend(bytes(BLOCK_SIZE * 4))
            worker = BackendWorker(backend, read_ahead_blocks=1)
            read = asyncio.create_task(worker.read_block(0))
            await asyncio.sleep(0.005)
            self.assertFalse(read.done())
            self.assertEqual(await read, bytes(BLOCK_SIZE))
            self.assertGreaterEqual(worker.max_service_latency, 0.05)
            self.assertGreaterEqual(worker.service_count, 1)
            await worker.close()
            self.assertTrue(backend.closed)

        asyncio.run(scenario())


class COBSRecoveryTestCase(unittest.TestCase):
    def test_decoder_resynchronizes_at_delimiter(self):
        decoder = gateware_cobs.Decoder(recover=True)
        expected = b"A\0B"

        async def sender(ctx):
            # A delimiter where a group header is required is malformed, then a complete valid
            # packet must still be accepted without resetting the decoder.
            for octet in b"\0" + gateware_cobs.encode(expected) + b"\0":
                ctx.set(decoder.i.payload, octet)
                ctx.set(decoder.i.valid, 1)
                await ctx.tick().until(decoder.i.ready)
                ctx.set(decoder.i.valid, 0)

        async def receiver(ctx):
            result = bytearray()
            ctx.set(decoder.o.ready, 1)
            while True:
                payload, = await ctx.tick().sample(decoder.o.payload).until(decoder.o.valid)
                if payload.end:
                    break
                result.append(payload.data)
            self.assertEqual(result, expected)

        simulator = Simulator(decoder)
        simulator.add_clock(1e-6)
        simulator.add_testbench(sender)
        simulator.add_testbench(receiver)
        simulator.run()


class ZaurusSDControllerTestCase(unittest.TestCase):
    def test_prepare_and_restore_are_idempotent(self):
        class FakeShell:
            def __init__(self):
                self.commands = []

            async def run(self, command, *, check=True, timeout=None):
                self.commands.append((command, check, timeout))
                return 0, ""

        async def scenario():
            shell = FakeShell()
            controller = ZaurusSDController(
                shell, "/usr/mnt.rom/card", settle_delay=0)
            await controller.prepare()
            prepare_commands = len(shell.commands)
            await controller.prepare()
            self.assertEqual(len(shell.commands), prepare_commands)
            self.assertTrue(any("killall sdmgr" in command
                for command, _, _ in shell.commands))
            self.assertTrue(any("rmmod sharp_mmcsd_m" in command
                for command, _, _ in shell.commands))
            self.assertTrue(any("insmod" in command
                for command, _, _ in shell.commands))

            await controller.restore()
            restore_commands = len(shell.commands)
            await controller.restore()
            self.assertEqual(len(shell.commands), restore_commands)
            self.assertTrue(any("/sbin/sdmgr" in command
                for command, _, _ in shell.commands[prepare_commands:]))
            self.assertTrue(all(timeout == 30 for _, _, timeout in shell.commands))

        asyncio.run(scenario())


class ConsoleTransportTestCase(unittest.TestCase):
    def test_telnet_username_negotiation(self):
        async def scenario():
            received = asyncio.get_running_loop().create_future()

            async def handler(reader, writer):
                writer.write(bytes((
                    TelnetTransport.IAC, TelnetTransport.DO,
                    TelnetTransport.NEW_ENVIRON,
                )))
                await writer.drain()
                negotiation = await reader.readexactly(3)
                writer.write(bytes((
                    TelnetTransport.IAC, TelnetTransport.SB,
                    TelnetTransport.NEW_ENVIRON, TelnetTransport.ENVIRON_SEND,
                    TelnetTransport.IAC, TelnetTransport.SE,
                )) + b"ready")
                await writer.drain()
                environment = await reader.readuntil(bytes((
                    TelnetTransport.IAC, TelnetTransport.SE,
                )))
                received.set_result((negotiation, environment))
                writer.close()
                await writer.wait_closed()

            server = await asyncio.start_server(handler, "127.0.0.1", 0)
            try:
                port = server.sockets[0].getsockname()[1]
                transport = await TelnetTransport.connect(
                    "127.0.0.1", port, username="zaurus")
                self.assertEqual(await transport.read_some(), b"ready")
                negotiation, environment = await received
                self.assertEqual(negotiation, bytes((
                    TelnetTransport.IAC, TelnetTransport.WILL,
                    TelnetTransport.NEW_ENVIRON,
                )))
                self.assertEqual(environment, bytes((
                    TelnetTransport.IAC, TelnetTransport.SB,
                    TelnetTransport.NEW_ENVIRON, TelnetTransport.ENVIRON_IS,
                    TelnetTransport.ENVIRON_VAR,
                )) + b"USER" + bytes((TelnetTransport.ENVIRON_VALUE,)) + \
                    b"zaurus" + bytes((TelnetTransport.IAC, TelnetTransport.SE)))
                await transport.close()
            finally:
                server.close()
                await server.wait_closed()

        asyncio.run(scenario())

    def test_telnet_negotiation_and_iac_escaping(self):
        async def scenario():
            received = asyncio.get_running_loop().create_future()

            async def handler(reader, writer):
                writer.write(bytes((
                    TelnetTransport.IAC, TelnetTransport.WILL, TelnetTransport.ECHO,
                    TelnetTransport.IAC, TelnetTransport.DO,
                    TelnetTransport.SUPPRESS_GO_AHEAD,
                )) + b"hello\xff\xff!")
                await writer.drain()
                negotiation = await reader.readexactly(6)
                application = await reader.readexactly(4)
                received.set_result((negotiation, application))
                writer.close()
                await writer.wait_closed()

            server = await asyncio.start_server(handler, "127.0.0.1", 0)
            try:
                port = server.sockets[0].getsockname()[1]
                transport = await TelnetTransport.connect("127.0.0.1", port)
                self.assertEqual(await transport.read_some(), b"hello\xff!")
                await transport.send(b"A\xffB")
                negotiation, application = await received
                self.assertEqual(negotiation, bytes((
                    TelnetTransport.IAC, TelnetTransport.DO, TelnetTransport.ECHO,
                    TelnetTransport.IAC, TelnetTransport.WILL,
                    TelnetTransport.SUPPRESS_GO_AHEAD,
                )))
                self.assertEqual(application, b"A\xff\xffB")
                await transport.close()
            finally:
                server.close()
                await server.wait_closed()

        asyncio.run(scenario())

    def test_serial_transport_over_pseudoterminal(self):
        async def scenario():
            master_fd, slave_fd = os.openpty()
            try:
                transport = await SerialTransport.connect(os.ttyname(slave_fd), 9600)
                await transport.send(b"host-to-target")
                self.assertEqual(os.read(master_fd, 14), b"host-to-target")
                os.write(master_fd, b"target-to-host")
                self.assertEqual(await transport.read_some(), b"target-to-host")
                await transport.close()
            finally:
                os.close(master_fd)
                os.close(slave_fd)

        asyncio.run(scenario())


class FileBlockBackendTestCase(unittest.TestCase):
    def test_file_backend_read_write_erase_and_flush(self):
        with tempfile.NamedTemporaryFile() as image:
            image.write(bytes([0x55]) * BLOCK_SIZE * 4)
            image.flush()
            backend = FileBlockBackend(image.name)
            try:
                self.assertEqual(backend.read_block(BLOCK_SIZE), bytes([0x55]) * BLOCK_SIZE)
                backend.write_block(BLOCK_SIZE, bytes([0xa5]) * BLOCK_SIZE)
                backend.erase(BLOCK_SIZE * 2, BLOCK_SIZE * 3)
                backend.flush()
                self.assertEqual(backend.read_block(BLOCK_SIZE), bytes([0xa5]) * BLOCK_SIZE)
                self.assertEqual(
                    backend.read_block(BLOCK_SIZE * 2), bytes([0xff]) * BLOCK_SIZE)
                self.assertEqual(
                    backend.read_block(BLOCK_SIZE * 3), bytes([0xff]) * BLOCK_SIZE)
            finally:
                backend.close()

    def test_file_backend_read_only(self):
        with tempfile.NamedTemporaryFile() as image:
            image.truncate(BLOCK_SIZE * 4)
            backend = FileBlockBackend(image.name, read_only=True)
            try:
                self.assertEqual(backend.read_block(0), bytes(BLOCK_SIZE))
                with self.assertRaises(BackendError) as context:
                    backend.write_block(0, bytes([0xa5]) * BLOCK_SIZE)
                self.assertEqual(context.exception.status, BackendStatus.ReadOnly)
            finally:
                backend.close()
