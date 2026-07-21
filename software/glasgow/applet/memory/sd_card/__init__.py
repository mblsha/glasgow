from collections.abc import Buffer
import asyncio
import enum
import logging
import secrets

from amaranth import *
from amaranth.lib import cdc, data, io, stream, wiring
from amaranth.lib.wiring import In, Out

from glasgow.abstract import AbstractAssembly, GlasgowPin
from glasgow.applet import GlasgowAppletError, GlasgowAppletV2
from glasgow.gateware.framing import COBSCRCFrameDecoder, COBSCRCFrameEncoder
from glasgow.gateware.stream import AsyncQueue
from glasgow.support.target_power import TargetPowerLifecycle

from ._backend import BackendError, BackendWorker, BlockBackend, FileBlockBackend
from ._buffer import BlockBuffer
from ._command import CommandEngine
from ._data import DataEngine
from ._fat16 import FAT16Error, FAT16Image, create_fat16_image
from ._gateware_support import (
    BLOCK_SIZE, FRAME_CRC16, IMAGE_SIZE_GRANULARITY, MAX_IMAGE_SIZE,
)
from ._host import HostBlockEngine
from ._protocol import (
    HOST_OPERATION_FOR_EVENT, BackendStatus, FrameError, HostEvent, HostReply,
    ReplyFrame, ReplyHeader, RequestFrame, RequestHeader,
)


__all__ = [
    "BackendError", "BackendStatus", "BackendWorker", "BlockBackend", "FileBlockBackend",
    "FAT16Error", "FAT16Image", "FrameError", "HostEvent", "HostReply",
    "RequestFrame", "RequestHeader",
    "ReplyFrame", "ReplyHeader",
    "CSDTransferSpeedProfile", "SDCardError", "SDCardComponent", "SDCardInterface",
    "SDCardApplet",
    "crc7", "crc16", "build_cid", "build_csd", "build_scr", "create_fat16_image",
]


class SDCardError(GlasgowAppletError):
    pass


class CSDTransferSpeedProfile(enum.IntEnum):
    """CSD ``TRAN_SPEED`` values offered by the emulator."""

    Default = 0x32       # 2.5 * 10 Mbit/s = 25 Mbit/s
    Conservative = 0x59  # 5.0 * 1 Mbit/s = 5 Mbit/s

    @property
    def description(self):
        if self is self.Conservative:
            return "conservative 5 Mbit/s"
        return "default 25 Mbit/s"


def _crc7_step(crc: int, bit: int) -> int:
    feedback = bit ^ ((crc >> 6) & 1)
    crc = (crc << 1) & 0x7f
    if feedback:
        crc ^= 0x09
    return crc


def crc7(value: int, width: int) -> int:
    """Return the SD CRC7 of *width* bits, processed most-significant bit first."""
    crc = 0
    for bitno in reversed(range(width)):
        crc = _crc7_step(crc, (value >> bitno) & 1)
    return crc


def _crc16_step(crc: int, bit: int) -> int:
    feedback = bit ^ ((crc >> 15) & 1)
    crc = (crc << 1) & 0xffff
    if feedback:
        crc ^= 0x1021
    return crc


def crc16(data: Buffer) -> int:
    """Return the SD CRC16-CCITT of *data*, processed most-significant bit first."""
    crc = 0
    for octet in data:
        for bitno in reversed(range(8)):
            crc = _crc16_step(crc, (octet >> bitno) & 1)
    return crc


def _set_field(value: int, high: int, low: int, field: int) -> int:
    width = high - low + 1
    assert field in range(1 << width)
    mask = ((1 << width) - 1) << low
    return (value & ~mask) | (field << low)


def _finish_register_crc(value: int) -> int:
    value = _set_field(value, 7, 1, crc7(value >> 8, 120))
    return _set_field(value, 0, 0, 1)


def build_cid(*, serial: int = 0x474c_4153) -> int:
    """Build a deterministic 128-bit CID register for the emulated card."""
    cid = 0
    cid = _set_field(cid, 127, 120, 0x03)
    cid = _set_field(cid, 119, 104, int.from_bytes(b"GL", "big"))
    cid = _set_field(cid, 103, 64, int.from_bytes(b"HOST ", "big"))
    cid = _set_field(cid, 63, 56, 0x10)
    cid = _set_field(cid, 55, 24, serial & 0xffff_ffff)
    cid = _set_field(cid, 19, 8, ((2026 - 2000) << 4) | 7)
    return _finish_register_crc(cid)


def _capacity_fields(size: int) -> tuple[int, int]:
    if size <= 0 or size > MAX_IMAGE_SIZE:
        raise SDCardError(
            f"image size must be between {IMAGE_SIZE_GRANULARITY} bytes and "
            f"{MAX_IMAGE_SIZE} bytes")
    if size % IMAGE_SIZE_GRANULARITY:
        raise SDCardError(
            f"image size must be a multiple of {IMAGE_SIZE_GRANULARITY} bytes")

    block_count = size // BLOCK_SIZE
    for c_size_mult in reversed(range(8)):
        multiplier = 1 << (c_size_mult + 2)
        if block_count % multiplier == 0:
            c_size = block_count // multiplier - 1
            if c_size in range(1 << 12):
                return c_size, c_size_mult
    raise SDCardError(f"image size {size} cannot be represented by a CSD v1 register")


def build_csd(size: int, *, read_only: bool = False,
        transfer_speed: CSDTransferSpeedProfile = CSDTransferSpeedProfile.Default) -> int:
    """Build a CSD v1 register describing a byte-addressed SDSC image."""
    c_size, c_size_mult = _capacity_fields(size)
    try:
        transfer_speed = CSDTransferSpeedProfile(transfer_speed)
    except ValueError as error:
        raise SDCardError(f"unsupported CSD transfer-speed profile {transfer_speed!r}") from error

    csd = 0
    csd = _set_field(csd, 127, 126, 0)       # CSD_STRUCTURE v1
    csd = _set_field(csd, 119, 112, 0x7f)    # TAAC: 80 ms
    csd = _set_field(csd, 111, 104, 0xff)    # NSAC: 25,500 clocks
    csd = _set_field(csd, 103, 96, transfer_speed)  # TRAN_SPEED
    csd = _set_field(csd, 95, 84, 0x135)     # classes 0, 2, 4, 5, 8
    csd = _set_field(csd, 83, 80, 9)         # READ_BL_LEN: 512 bytes
    csd = _set_field(csd, 79, 79, 0)         # fixed-size reads only
    csd = _set_field(csd, 78, 78, 0)         # no write misalignment
    csd = _set_field(csd, 77, 77, 0)         # no read misalignment
    csd = _set_field(csd, 76, 76, 0)         # no DSR
    csd = _set_field(csd, 73, 62, c_size)
    csd = _set_field(csd, 49, 47, c_size_mult)
    csd = _set_field(csd, 46, 46, 1)         # single-block erase
    csd = _set_field(csd, 45, 39, 0)         # one write block per erase sector
    csd = _set_field(csd, 31, 31, 0)         # no write-protect groups
    csd = _set_field(csd, 28, 26, 7)         # conservative write-time factor
    csd = _set_field(csd, 25, 22, 9)         # WRITE_BL_LEN: 512 bytes
    csd = _set_field(csd, 21, 21, 0)         # fixed-size writes only
    csd = _set_field(csd, 15, 15, 0)
    csd = _set_field(csd, 12, 12, read_only)  # temporary write protection
    csd = _set_field(csd, 11, 10, 0)         # partitioned/DOS-style image
    return _finish_register_crc(csd)


def build_scr() -> int:
    """Build an SD 1.01 SCR supporting both 1-bit and 4-bit buses."""
    scr = 0
    scr = _set_field(scr, 63, 60, 0)         # SCR_STRUCTURE v1
    scr = _set_field(scr, 59, 56, 0)         # SD_SPEC 1.0/1.01
    scr = _set_field(scr, 55, 55, 1)         # erased data reads as ones
    scr = _set_field(scr, 54, 52, 0)         # no security protocol
    scr = _set_field(scr, 51, 48, 0b0101)    # 1-bit and 4-bit buses
    return scr


class SDCardComponent(wiring.Component):
    i_stream: In(stream.Signature(8))
    o_stream: Out(stream.Signature(8))
    o_flush:  Out(1)

    enabled:        In(1)
    generation:     In(16)
    read_only:      In(1)
    image_size:     In(25)
    cid:            In(128)
    csd:            In(128)
    scr:            In(64)

    last_command:   Out(6)
    clock_count:    Out(32)
    command_low_count: Out(32)
    command_count:  Out(32)
    read_count:     Out(32)
    write_count:    Out(32)
    protocol_errors: Out(16)
    backend_errors: Out(16)
    stale_replies:  Out(16)
    buffer_faults:  Out(16)
    prefetch_waits: Out(32)
    max_host_service_latency: Out(32)

    def __init__(self, ports):
        self._ports = ports
        super().__init__()

    def elaborate(self, platform):
        m = Module()

        m.submodules.clk_buffer = clk_buffer = io.Buffer("i", self._ports.clk)
        m.submodules.cmd_buffer = cmd_buffer = io.Buffer("io", self._ports.cmd)
        dat_buffers = []
        for index, dat_port in enumerate(self._ports.dat):
            buffer = io.Buffer("io", dat_port)
            m.submodules[f"dat_buffer_{index}"] = buffer
            dat_buffers.append(buffer)

        if platform is not None:
            platform.add_clock_constraint(clk_buffer.i, 25e6)

        m.domains.sd = cd_sd = ClockDomain(async_reset=True, local=True)
        m.domains.sd_fall = cd_sd_fall = ClockDomain(async_reset=True, local=True)
        m.d.comb += [
            cd_sd.clk.eq(clk_buffer.i),
            cd_sd.rst.eq(ResetSignal() | ~self.enabled),
            cd_sd_fall.clk.eq(~clk_buffer.i),
            cd_sd_fall.rst.eq(ResetSignal() | ~self.enabled),
        ]

        enabled_sd = Signal()
        m.submodules.enabled_sync = cdc.FFSynchronizer(self.enabled, enabled_sd, o_domain="sd")

        # Configuration registers are written while the SD domain is held in reset. Once enable
        # has crossed into the target-clock domain, take one coherent snapshot before allowing
        # either command sampling or data output. The wide buses remain stable for the lifetime
        # of this snapshot and are never consumed directly across the domain boundary.
        configuration = Signal(data.StructLayout({
            "generation": 16,
            "read_only": 1,
            "image_size": 25,
            "cid": 128,
            "csd": 128,
            "scr": 64,
        }))
        configuration_valid = Signal()
        with m.If(enabled_sd & ~configuration_valid):
            m.d.sd += [
                configuration.generation.eq(self.generation),
                configuration.read_only.eq(self.read_only),
                configuration.image_size.eq(self.image_size),
                configuration.cid.eq(self.cid),
                configuration.csd.eq(self.csd),
                configuration.scr.eq(self.scr),
                configuration_valid.eq(1),
            ]
        engine_enabled = enabled_sd & configuration_valid

        m.submodules.host_decoder = host_decoder = COBSCRCFrameDecoder(FRAME_CRC16)
        wiring.connect(m, host_decoder.i, wiring.flipped(self.i_stream))

        host_to_sd = AsyncQueue(
            shape=data.StructLayout({"data": 8, "end": 1, "error": 1}),
            depth=1024, i_domain="sync", o_domain="sd")
        sd_to_host = AsyncQueue(
            shape=data.StructLayout({"data": 8, "end": 1}),
            depth=64, i_domain="sd", o_domain="sync")
        m.submodules.host_to_sd = host_to_sd
        m.submodules.sd_to_host = sd_to_host
        wiring.connect(m, host_to_sd.i, host_decoder.o)

        m.submodules.host_encoder = host_encoder = COBSCRCFrameEncoder(
            FRAME_CRC16, fifo_depth=1024)
        wiring.connect(m, host_encoder.i, sd_to_host.o)
        wiring.connect(m, wiring.flipped(self.o_stream), host_encoder.o)
        m.d.sync += self.o_flush.eq(0)
        with m.If(self.o_stream.valid & self.o_stream.ready & (self.o_stream.payload == 0)):
            m.d.sync += self.o_flush.eq(1)

        m.submodules.block_buffer = block_buffer = BlockBuffer()

        dat_i = Cat(*(buffer.i for buffer in dat_buffers))
        m.submodules.command_engine = command_engine = CommandEngine()
        m.submodules.data_engine = data_engine = DataEngine()
        m.submodules.host_engine = host_engine = HostBlockEngine()

        wiring.connect(m, command_engine.control, data_engine.control)
        wiring.connect(m, data_engine.storage, host_engine.storage)
        wiring.connect(m, host_engine.buffer, block_buffer.host)
        wiring.connect(m, data_engine.buffer, block_buffer.data)
        wiring.connect(m, host_engine.i_stream, host_to_sd.o)
        wiring.connect(m, sd_to_host.i, host_engine.o_stream)

        m.d.comb += [
            command_engine.cmd_i.eq(cmd_buffer.i),
            cmd_buffer.o.eq(command_engine.cmd_o),
            cmd_buffer.oe.eq(command_engine.cmd_oe),
            command_engine.enabled.eq(engine_enabled & ~block_buffer.fault),
            command_engine.read_only.eq(configuration.read_only),
            command_engine.image_size.eq(configuration.image_size),
            command_engine.cid.eq(configuration.cid),
            command_engine.csd.eq(configuration.csd),

            data_engine.dat_i.eq(dat_i),
            data_engine.scr.eq(configuration.scr),
            data_engine.image_size.eq(configuration.image_size),

            host_engine.generation.eq(configuration.generation),
            host_engine.current_address.eq(command_engine.control.current_address),
            host_engine.erase_start.eq(command_engine.control.erase_start),
            host_engine.erase_end.eq(command_engine.control.erase_end),

            self.last_command.eq(command_engine.last_command),
            self.clock_count.eq(command_engine.clock_count),
            self.command_low_count.eq(command_engine.command_low_count),
            self.command_count.eq(command_engine.command_count),
            self.read_count.eq(data_engine.read_count),
            self.write_count.eq(data_engine.write_count),
            self.protocol_errors.eq(
                command_engine.protocol_errors + data_engine.protocol_errors +
                host_engine.protocol_errors + block_buffer.collision_count),
            self.backend_errors.eq(host_engine.backend_errors),
            self.stale_replies.eq(host_engine.stale_replies),
            self.buffer_faults.eq(block_buffer.collision_count),
            self.prefetch_waits.eq(data_engine.prefetch_waits),
            self.max_host_service_latency.eq(host_engine.max_host_service_latency),
        ]

        dat_o_latched = Signal(4, init=0xf)
        dat_oe_latched = Signal(4)
        m.d.sd_fall += [
            dat_o_latched.eq(data_engine.dat_o),
            dat_oe_latched.eq(Mux(
                engine_enabled & ~block_buffer.fault, data_engine.dat_oe, 0)),
        ]
        for index, buffer in enumerate(dat_buffers):
            m.d.comb += [
                buffer.o.eq(dat_o_latched[index]),
                buffer.oe.eq(dat_oe_latched[index]),
            ]

        return m


class SDCardInterface:
    def __init__(self, logger: logging.Logger, assembly: AbstractAssembly, *,
            clk: GlasgowPin, cmd: GlasgowPin, dat: tuple[GlasgowPin, ...],
            worker_factory=BackendWorker):
        self._logger = logger
        self._level = logging.DEBUG if self._logger.name == __name__ else logging.TRACE

        ports = assembly.add_port_group(clk=clk, cmd=cmd, dat=dat)
        assembly.use_pulls({cmd: "high", dat: "high"})
        component = assembly.add_submodule(SDCardComponent(ports))
        self._pipe = assembly.add_inout_pipe(
            component.o_stream, component.i_stream, in_flush=component.o_flush,
            in_fifo_depth=0, out_fifo_depth=0,
            in_buffer_size=4096, out_buffer_size=4096)

        self._enabled = assembly.add_rw_register(component.enabled)
        self._generation_reg = assembly.add_rw_register(component.generation)
        self._read_only_reg = assembly.add_rw_register(component.read_only)
        self._image_size = assembly.add_rw_register(component.image_size)
        self._cid = assembly.add_rw_register(component.cid)
        self._csd = assembly.add_rw_register(component.csd)
        self._scr = assembly.add_rw_register(component.scr)
        self.last_command = assembly.add_ro_register(component.last_command)
        self.clock_count = assembly.add_ro_register(component.clock_count)
        self.command_low_count = assembly.add_ro_register(component.command_low_count)
        self.command_count = assembly.add_ro_register(component.command_count)
        self.read_count = assembly.add_ro_register(component.read_count)
        self.write_count = assembly.add_ro_register(component.write_count)
        self.protocol_errors = assembly.add_ro_register(component.protocol_errors)
        self.backend_errors = assembly.add_ro_register(component.backend_errors)
        self.stale_replies = assembly.add_ro_register(component.stale_replies)
        self.buffer_faults = assembly.add_ro_register(component.buffer_faults)
        self.prefetch_waits = assembly.add_ro_register(component.prefetch_waits)
        self.max_host_service_latency = assembly.add_ro_register(
            component.max_host_service_latency)

        self._backend = None
        self._worker = None
        self._worker_factory = worker_factory
        self._generation = 0
        self._sync_writes = True
        self._active = False
        self._flush_count = 0
        self._host_backend_errors = 0

    @property
    def backend(self) -> BlockBackend | None:
        return self._backend

    def _next_generation(self) -> int:
        # Randomizing the initial value also protects against replies retained when the same
        # bitstream is reused by a later applet process.
        generation = secrets.randbits(16)
        while generation == 0 or generation == self._generation:
            generation = secrets.randbits(16)
        self._generation = generation
        return generation

    async def configure(self, image_path: str | None = None, *,
            backend: BlockBackend | None = None, read_only: bool = False,
            sync_writes: bool = True,
            transfer_speed: CSDTransferSpeedProfile = CSDTransferSpeedProfile.Default):
        if (image_path is None) == (backend is None):
            raise ValueError("exactly one of image_path or backend must be provided")
        if backend is None:
            backend = FileBlockBackend(image_path, read_only=read_only)
        worker = self._worker_factory(backend)
        try:
            size = backend.size
            transfer_speed = CSDTransferSpeedProfile(transfer_speed)
            csd = build_csd(
                size, read_only=backend.read_only, transfer_speed=transfer_speed)
            cid = build_cid(serial=backend.identity)
            scr = build_scr()

            await self._enabled.set(0)
            await self._pipe.reset()
            await self._generation_reg.set(self._next_generation())
            await self._read_only_reg.set(backend.read_only)
            await self._image_size.set(size)
            await self._cid.set(cid)
            await self._csd.set(csd)
            await self._scr.set(scr)
            await self._enabled.set(1)
            self._active = True
        except BaseException:
            await worker.close()
            raise

        if self._worker is not None:
            await self._worker.close()
        self._backend = backend
        self._worker = worker
        self._sync_writes = sync_writes
        access = "read-only" if backend.read_only else "writable"
        durability = "synchronous" if sync_writes else "write-back"
        self._logger.info(
            "serving %s as a %.2f MiB SDSC image (%s, %s, %s CSD)",
            getattr(backend, "path", backend.__class__.__name__), size / 2**20,
            access, durability, transfer_speed.description)

    async def suspend(self, *, reason: str = "requested"):
        if not self._active:
            return
        self._logger.info("suspending SD card: %s", reason)
        await self._enabled.set(0)
        self._active = False
        await self._generation_reg.set(self._next_generation())
        await self._pipe.reset()

    async def resume(self, *, reason: str = "requested"):
        if self._backend is None or self._active:
            return
        await self._pipe.reset()
        await self._generation_reg.set(self._next_generation())
        await self._enabled.set(1)
        self._active = True
        self._logger.info("resumed SD card: %s (generation %#06x)", reason, self._generation)

    async def monitor_power(self, device, port, *, off_threshold: float = 1.0,
            on_threshold: float = 1.8, interval: float = 0.25, debounce_samples: int = 3):
        async def flush_after_suspend():
            try:
                await self.flush()
            except BackendError as error:
                self._record_backend_error("power-loss flush", error)

        try:
            monitor = TargetPowerLifecycle(
                self._logger,
                sample_voltage=lambda: device.measure_voltage(str(port)),
                is_active=lambda: self._active,
                after_suspend=flush_after_suspend,
                suspend=lambda voltage: self.suspend(
                    reason=f"target rail fell to {voltage:.2f} V"),
                resume=lambda voltage: self.resume(
                    reason=f"target rail rose to {voltage:.2f} V"),
                off_threshold=off_threshold,
                on_threshold=on_threshold,
                interval=interval,
                debounce_samples=debounce_samples,
                description=f"port {port} target power",
            )
            await monitor.run()
        except ValueError as error:
            raise SDCardError(str(error)) from error

    async def close(self):
        statistics = None
        if self._backend is not None:
            statistics = (
                await self.clock_count, await self.command_low_count,
                await self.command_count, await self.read_count,
                await self.write_count, await self.protocol_errors,
                await self.backend_errors, await self.stale_replies,
                await self.buffer_faults, await self.prefetch_waits,
                await self.max_host_service_latency,
            )
        await self._enabled.set(0)
        self._active = False
        if statistics is not None:
            self._logger.info(
                "stopped after %d SD clocks (%d CMD-low), %d commands, %d reads, %d writes, "
                "%d protocol errors, %d FPGA-reported backend errors, %d stale replies; "
                "%d buffer faults, %d prefetch waits, %d max host-service SD clocks; "
                "%d host backend errors, %d durable flushes, %.3f ms max backend call",
                *statistics, self._host_backend_errors, self._flush_count,
                getattr(self._worker, "max_service_latency", 0.0) * 1e3)
        if self._backend is not None:
            try:
                await self._worker.flush()
            except BackendError as error:
                self._record_backend_error("close flush", error)
            await self._worker.close()
            self._worker = None
            self._backend = None

    def _record_backend_error(self, operation: str, error: BackendError):
        self._host_backend_errors += 1
        self._logger.error("host-backed SD %s failed (%s): %s",
            operation, error.status.name, error)

    async def flush(self):
        if self._worker is None:
            raise SDCardError("no image is configured")
        await self._worker.flush()
        self._flush_count += 1

    async def _reply(self, opcode: HostReply, generation: int, request_id: int,
            status: BackendStatus = BackendStatus.OK, payload=b""):
        packet = ReplyFrame.create(
            opcode, generation, request_id, status, payload).encode_packet()
        await self._pipe.send(packet)
        await self._pipe.flush()

    async def serve_once(self):
        if self._backend is None:
            raise SDCardError("no image is configured")

        while True:
            packet = await self._pipe.recv_until(b"\0")
            try:
                request = RequestFrame.decode_packet(packet)
            except FrameError as error:
                self._logger.warning("discarding malformed SD backend frame: %s", error)
                continue
            break

        header = request.header
        payload = request.payload
        if header.generation != self._generation:
            self._logger.debug(
                "discarding stale %s request %#06x/%#06x",
                header.opcode.name.lower(), header.generation, header.request_id)
            return
        operation = HOST_OPERATION_FOR_EVENT[header.opcode]
        try:
            fields = operation.request_payload.decode(payload)
        except FrameError:
            self._logger.warning(
                "invalid %s request length %d (expected %d)",
                header.opcode.name.lower(), len(payload), operation.request_payload.size)
            await self._reply(
                operation.reply, header.generation, header.request_id,
                BackendStatus.Invalid, operation.invalid_reply_payload())
            return

        if header.opcode == HostEvent.Read:
            address = fields["address"]
            self._logger.log(self._level, "read block at %#x", address)
            status = BackendStatus.OK
            try:
                block = await self._worker.read_block(address)
            except BackendError as error:
                self._record_backend_error("read", error)
                status = error.status
                block = bytes([0xff]) * BLOCK_SIZE
            await self._reply(
                operation.reply, header.generation, header.request_id, status,
                operation.reply_payload.encode(data=block))

        elif header.opcode == HostEvent.Write:
            address = fields["address"]
            block = fields["data"]
            self._logger.log(self._level, "write block at %#x", address)
            status = BackendStatus.OK
            try:
                await self._worker.write_block(address, block)
                if self._sync_writes:
                    await self.flush()
            except BackendError as error:
                self._record_backend_error("write", error)
                status = error.status
            await self._reply(
                operation.reply, header.generation, header.request_id, status)

        elif header.opcode == HostEvent.Erase:
            start, end = fields["start"], fields["end"]
            self._logger.log(self._level, "erase blocks at %#x..%#x", start, end)
            status = BackendStatus.OK
            try:
                await self._worker.erase(start, end)
                if self._sync_writes:
                    await self.flush()
            except BackendError as error:
                self._record_backend_error("erase", error)
                status = error.status
            await self._reply(
                operation.reply, header.generation, header.request_id, status)

        else:
            raise SDCardError(f"unknown FPGA event opcode {header.opcode:#04x}")

    async def serve(self):
        try:
            while True:
                await self.serve_once()
        finally:
            await self.close()


class SDCardApplet(GlasgowAppletV2):
    logger = logging.getLogger(__name__)
    help = "emulate a host-backed SD memory card"
    description = """
    Emulate a byte-addressed SDSC memory card using a disk image on the host.

    The FPGA stores two 512-byte ping-pong data blocks and bounded transport FIFOs. Read blocks
    are fetched from the host during the card access delay, and write blocks keep DAT0 busy until
    the host has written and synchronized them to the image. Images must be 2 KiB aligned and no
    larger than 16 MiB. Target-power monitoring automatically resets protocol and transport state
    when a device such as a Zaurus removes and restores its SD rail.

    The default pins match the SD-to-Glasgow breakout on port A. For port B, use ``--clk B2
    --cmd B5 --dat B1,B0,B7,B6``. The DAT pins are specified in DAT0,DAT1,DAT2,DAT3 order.
    """
    required_revision = "C0"
    preview = True

    @classmethod
    def add_build_arguments(cls, parser, access):
        access.add_voltage_argument(parser)
        access.add_pins_argument(parser, "clk", default="A2", required=True)
        access.add_pins_argument(parser, "cmd", default="A5", required=True)
        access.add_pins_argument(
            parser, "dat", width=4, default="A1,A0,A7,A6", required=True,
            help="bind DAT0,DAT1,DAT2,DAT3 in that order")

    def build(self, args):
        with self.assembly.add_applet(self):
            self.assembly.use_voltage(args.voltage)
            self.sd_card_iface = SDCardInterface(self.logger, self.assembly,
                clk=args.clk, cmd=args.cmd, dat=args.dat)
            self._sd_port = args.clk.port

    @classmethod
    def add_setup_arguments(cls, parser):
        parser.add_argument("image", metavar="IMAGE",
            help="serve IMAGE as a writable SDSC card (2 KiB aligned, at most 16 MiB)")
        parser.add_argument("--read-only", action="store_true",
            help="advertise write protection and open IMAGE read-only")
        parser.add_argument("--volatile-writes", action="store_true",
            help="acknowledge writes before fsync (the image is still flushed on exit)")
        parser.add_argument("--conservative-speed", action="store_true",
            help="advertise a conservative 5 Mbit/s CSD transfer speed for compatibility tests")
        parser.add_argument("--no-power-monitor", action="store_true",
            help="do not reset the applet when the CLK port target rail changes")
        parser.add_argument("--power-off-threshold", type=float, default=1.0, metavar="V",
            help="treat the target rail as off below V (default: 1.0)")
        parser.add_argument("--power-on-threshold", type=float, default=1.8, metavar="V",
            help="treat the target rail as on above V (default: 1.8)")
        parser.add_argument("--power-poll-interval", type=float, default=0.25, metavar="SECONDS",
            help="target-power polling interval (default: 0.25)")
        parser.add_argument("--power-debounce-samples", type=int, default=3, metavar="COUNT",
            help="consecutive samples required for a power transition (default: 3)")

    async def setup(self, args):
        try:
            await self.sd_card_iface.configure(
                args.image, read_only=args.read_only, sync_writes=not args.volatile_writes,
                transfer_speed=(CSDTransferSpeedProfile.Conservative
                    if args.conservative_speed else CSDTransferSpeedProfile.Default))
        except OSError as error:
            raise SDCardError(f"cannot open image {args.image!r}: {error.strerror}") from error

    async def run(self, args):
        if args.no_power_monitor:
            await self.sd_card_iface.serve()
            return
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(self.sd_card_iface.serve())
            tasks.create_task(self.sd_card_iface.monitor_power(
                self.device, self._sd_port,
                off_threshold=args.power_off_threshold,
                on_threshold=args.power_on_threshold,
                interval=args.power_poll_interval,
                debounce_samples=args.power_debounce_samples))

    @classmethod
    def tests(cls):
        from . import test
        return test.SDCardAppletTestCase
