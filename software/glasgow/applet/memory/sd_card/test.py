import asyncio
import os
import struct
import tempfile
import unittest

from glasgow.applet import GlasgowAppletV2TestCase, synthesis_test
from glasgow.simulation.assembly import SimulationAssembly

from . import (
    BLOCK_SIZE, BackendError, BackendStatus, BlockBackend, FileBlockBackend, HostEvent, HostReply,
    ReplyHeader, RequestHeader, SDCardApplet, SDCardInterface,
    build_cid, build_csd, build_scr, crc7, crc16,
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

    def test_sim_initialization_and_single_block_read_write(self):
        payload = bytes(
            ((index // BLOCK_SIZE) * 0x31 + index) & 0xff
            for index in range(BLOCK_SIZE * 4))
        with tempfile.NamedTemporaryFile() as image:
            image.write(payload)
            image.flush()

            args = self._parse_args([image.name])
            assembly = SimulationAssembly()
            applet = self.applet_cls(assembly)
            applet.build(args)

            clk = assembly.get_pin("A2")
            cmd = assembly.get_pin("A5")
            dat = [assembly.get_pin(name) for name in ("A1", "A0", "A7", "A6")]

            async def card_host(ctx):
                ctx.set(clk.i, 0)
                ctx.set(cmd.i, 1)
                for pin in dat:
                    ctx.set(pin.i, 1)

                async def cycle(command_bit=1):
                    ctx.set(cmd.i, command_bit)
                    await ctx.delay(250e-9)
                    ctx.set(clk.i, 1)
                    await ctx.delay(250e-9)
                    sample = (
                        ctx.get(cmd.oe), ctx.get(cmd.o),
                        tuple((ctx.get(pin.oe), ctx.get(pin.o)) for pin in dat),
                    )
                    ctx.set(clk.i, 0)
                    await ctx.delay(250e-9)
                    return sample

                async def send_command(index, argument=0):
                    protected = (1 << 38) | (index << 32) | argument
                    frame = (protected << 8) | (crc7(protected, 40) << 1) | 1
                    for bitno in reversed(range(48)):
                        await cycle((frame >> bitno) & 1)

                async def receive_response(length):
                    bits = []
                    for _ in range(length + 16):
                        command_oe, command_o, _ = await cycle()
                        if command_oe:
                            bits.append(command_o)
                            if len(bits) == length:
                                break
                    self.assertEqual(len(bits), length)
                    return sum(bit << bitno for bitno, bit in enumerate(reversed(bits)))

                def assert_r1(response, index):
                    self.assertEqual(response >> 46, 0)
                    self.assertEqual((response >> 40) & 0x3f, index, hex(response))
                    self.assertEqual((response >> 1) & 0x7f, crc7(response >> 8, 40))
                    self.assertEqual(response & 1, 1)

                async def receive_data_block(expected):
                    # The host-backed block may take many SD clocks to cross the USB-side queues.
                    for _ in range(2048):
                        _, _, data_sample = await cycle()
                        if data_sample[0] == (1, 0):
                            break
                    else:
                        pipe = applet.sd_card_iface._pipe
                        self.fail(
                            "timed out waiting for SD data start bit "
                            f"(host in={len(pipe._i_buffer)}, host out={len(pipe._o_buffer)}, "
                            f"errors={await applet.sd_card_iface.protocol_errors})")

                    block = 0
                    for _ in range(BLOCK_SIZE * 8):
                        _, _, data_sample = await cycle()
                        self.assertEqual(data_sample[0][0], 1)
                        block = (block << 1) | data_sample[0][1]
                    block = block.to_bytes(BLOCK_SIZE, "big")
                    self.assertEqual(block, expected)

                    received_crc = 0
                    for _ in range(16):
                        _, _, data_sample = await cycle()
                        received_crc = (received_crc << 1) | data_sample[0][1]
                    self.assertEqual(received_crc, crc16(block))

                    _, _, data_sample = await cycle()
                    self.assertEqual(data_sample[0], (1, 1))

                # Let the enable synchronizer cross into the externally clocked SD domain.
                for _ in range(4):
                    await cycle()

                await send_command(0)
                for _ in range(4):
                    command_oe, _, _ = await cycle()
                    self.assertEqual(command_oe, 0)

                await send_command(55)
                self.assertEqual(await applet.sd_card_iface.last_command, 55)
                assert_r1(await receive_response(48), 55)

                await send_command(41, 0x00ff_8000)
                r3 = await receive_response(48)
                self.assertEqual(r3 >> 46, 0)
                self.assertEqual((r3 >> 40) & 0x3f, 0x3f)
                self.assertEqual((r3 >> 8) & 0xffff_ffff, 0x80ff_8000)
                self.assertEqual(r3 & 0xff, 0xff)

                stat = os.fstat(image.fileno())
                cid = build_cid(serial=(stat.st_ino ^ stat.st_size) & 0xffff_ffff)
                csd = build_csd(len(payload))

                await send_command(2)
                r2 = await receive_response(136)
                self.assertEqual((r2 >> 1) & ((1 << 127) - 1), cid >> 1)

                await send_command(3)
                r6 = await receive_response(48)
                self.assertEqual((r6 >> 24) & 0xffff, 0x1234)
                self.assertEqual((r6 >> 40) & 0x3f, 3)

                await send_command(9, 0x1234_0000)
                r2 = await receive_response(136)
                self.assertEqual((r2 >> 1) & ((1 << 127) - 1), csd >> 1)

                await send_command(7, 0x1234_0000)
                assert_r1(await receive_response(48), 7)

                await send_command(16, BLOCK_SIZE)
                assert_r1(await receive_response(48), 16)

                await send_command(17, BLOCK_SIZE)
                assert_r1(await receive_response(48), 17)

                while applet.sd_card_iface._pipe.readable == 0:
                    await cycle()
                await applet.sd_card_iface.serve_once()
                await receive_data_block(payload[BLOCK_SIZE:BLOCK_SIZE * 2])

                # A multi-block read prefetches its next block. If CMD12 stops it before that
                # block is transferred, the following command must wait for data tagged with its
                # own address rather than consuming the stale prefetch.
                await send_command(18, 0)
                assert_r1(await receive_response(48), 18)
                while applet.sd_card_iface._pipe.readable == 0:
                    await cycle()
                await applet.sd_card_iface.serve_once()
                await receive_data_block(payload[:BLOCK_SIZE])

                await send_command(12)
                assert_r1(await receive_response(48), 12)
                while applet.sd_card_iface._pipe.readable == 0:
                    await cycle()
                await applet.sd_card_iface.serve_once()  # stale block-one prefetch

                read_address = BLOCK_SIZE * 3
                await send_command(17, read_address)
                assert_r1(await receive_response(48), 17)
                while applet.sd_card_iface._pipe.readable == 0:
                    await cycle()
                await applet.sd_card_iface.serve_once()
                await receive_data_block(payload[read_address:read_address + BLOCK_SIZE])

                await send_command(55)
                assert_r1(await receive_response(48), 55)
                await send_command(6, 2)
                assert_r1(await receive_response(48), 6)

                write_address = BLOCK_SIZE * 2
                write_block = bytes((0xa5 ^ index) & 0xff for index in range(BLOCK_SIZE))
                await send_command(24, write_address)
                assert_r1(await receive_response(48), 24)

                def drive_data(value):
                    for lane, pin in enumerate(dat):
                        ctx.set(pin.i, (value >> lane) & 1)

                lane_bits = [[] for _ in range(4)]
                # Leave the required write-response-to-data gap before the start bit.
                drive_data(0xf)
                await cycle()
                await cycle()
                drive_data(0)
                await cycle()  # start bit on all four lanes
                for octet in write_block:
                    for nibble in (octet >> 4, octet & 0xf):
                        drive_data(nibble)
                        await cycle()
                        for lane in range(4):
                            lane_bits[lane].append((nibble >> lane) & 1)

                lane_crcs = []
                for bits in lane_bits:
                    lane_data = bytes(
                        sum(bit << (7 - index) for index, bit in enumerate(bits[offset:offset + 8]))
                        for offset in range(0, len(bits), 8))
                    lane_crcs.append(crc16(lane_data))
                for bitno in reversed(range(16)):
                    drive_data(sum(((lane_crcs[lane] >> bitno) & 1) << lane
                                   for lane in range(4)))
                    await cycle()

                drive_data(0xf)
                await cycle()  # end bit on all four lanes

                response = []
                for _ in range(32):
                    _, _, data_sample = await cycle()
                    if data_sample[0][0]:
                        response.append(data_sample[0][1])
                        if len(response) == 5:
                            break
                self.assertEqual(response, [0, 0, 1, 0, 1])

                # DAT0 remains low while the block crosses to the host and is written.
                saw_busy = False
                for _ in range(4096):
                    _, _, data_sample = await cycle()
                    if data_sample[0] == (1, 0):
                        saw_busy = True
                    if applet.sd_card_iface._pipe.readable:
                        await applet.sd_card_iface.serve_once()
                    if saw_busy and data_sample[0][0] == 0:
                        break
                else:
                    pipe = applet.sd_card_iface._pipe
                    self.fail(
                        "timed out waiting for host-backed write completion "
                        f"(host in={len(pipe._i_buffer)}, host out={len(pipe._o_buffer)}, "
                        f"errors={await applet.sd_card_iface.protocol_errors})")
                self.assertTrue(saw_busy)
                self.assertEqual(os.pread(image.fileno(), BLOCK_SIZE, write_address), write_block)

                await applet.sd_card_iface.close()

            async def main(ctx):
                await applet.setup(args)
                await card_host(ctx)

            assembly.run(main)


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


class SDCardInterfaceTestCase(unittest.TestCase):
    def make_iface(self, backend, request, *, generation=GENERATION, sync_writes=True):
        iface = object.__new__(SDCardInterface)
        iface._backend = backend
        iface._generation = generation
        iface._sync_writes = sync_writes
        iface._flush_count = 0
        iface._host_backend_errors = 0
        iface._pipe = _FakePipe(request)
        iface._logger = __import__("logging").getLogger(__name__)
        iface._level = __import__("logging").DEBUG
        return iface

    def request(self, opcode, payload=b"", *, generation=GENERATION, request_id=REQUEST_ID):
        return RequestHeader(opcode, generation, request_id).encode() + payload

    def reply(self, pipe):
        header = ReplyHeader.decode(bytes(pipe.replies[:ReplyHeader.size]))
        payload = bytes(pipe.replies[ReplyHeader.size:])
        return header, payload

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
            self.assertEqual(transitions, ["flush", "suspend", "resume"])

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
