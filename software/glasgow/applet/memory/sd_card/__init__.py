import asyncio
from dataclasses import dataclass, replace
import logging
import secrets

from amaranth import *
from amaranth.lib import cdc, data, io, stream, wiring
from amaranth.lib.wiring import In, Out

from glasgow.abstract import AbstractAssembly, GlasgowPin
from glasgow.applet import GlasgowAppletV2
from glasgow.gateware.framing import COBSCRCFrameDecoder, COBSCRCFrameEncoder
from glasgow.gateware.stream import AsyncQueue
from glasgow.support.target_power import TargetPowerLifecycle

from ._backend import BackendError, BackendWorker, BlockBackend, FileBlockBackend
from ._buffer import BlockBuffer
from ._command import CommandEngine
from ._data import DataEngine
from ._diagnostics import FPGA_DIAGNOSTICS_LAYOUT, decode_fpga_diagnostics
from ._errors import SDCardError
from ._fat16 import FAT16Error, FAT16Image, create_fat16_image
from ._gateware_support import (
    BLOCK_SIZE, FRAME_CRC16, IMAGE_SIZE_WIDTH,
)
from ._host import HostBlockEngine
from ._protocol import (
    HOST_OPERATION_FOR_EVENT, BackendStatus, FrameError, HostEvent, HostReply,
    ReplyFrame, ReplyHeader, RequestFrame, RequestHeader,
)
from ._registers import (
    DEFAULT_TRANSFER_SPEED, build_cid, build_csd, build_scr, crc7, crc16,
    decode_csd_transfer_speed, encode_csd_transfer_speed, parse_transfer_speed,
)
from ._reply_packer import (
    HostReplyWordPacker, HOST_REPLY_WORD_LAYOUT,
    RawBlockWordPacker, RAW_BLOCK_WORD_LAYOUT,
)


__all__ = [
    "BackendError", "BackendStatus", "BackendWorker", "BlockBackend", "FileBlockBackend",
    "FAT16Error", "FAT16Image", "FrameError", "HostEvent", "HostReply",
    "RequestFrame", "RequestHeader",
    "ReplyFrame", "ReplyHeader",
    "DEFAULT_TRANSFER_SPEED", "SDCardDiagnostics", "SDCardError", "SDCardComponent",
    "SDCardInterface", "SDCardApplet",
    "crc7", "crc16", "build_cid", "build_csd", "build_scr", "create_fat16_image",
    "decode_csd_transfer_speed", "encode_csd_transfer_speed", "parse_transfer_speed",
]


@dataclass(frozen=True)
class SDCardDiagnostics:
    sd_clocks: int
    command_low_clocks: int
    commands: int
    reads: int
    writes: int
    protocol_errors: int
    fpga_backend_errors: int
    stale_replies: int
    buffer_faults: int
    prefetch_waits: int
    prefetch_wait_cycles: int
    max_prefetch_wait_cycles: int
    max_host_service_latency_cycles: int
    host_backend_errors: int
    durable_flushes: int
    backend_calls: int
    max_backend_call_seconds: float
    card_state: int
    data_mode: int
    read_bank_states: int
    host_transaction_phase: int
    host_reply_header_bytes: int
    host_reply_payload_bytes: int


@dataclass(frozen=True)
class _BackendSession:
    backend: BlockBackend
    worker: BackendWorker
    sync_writes: bool


class SDCardComponent(wiring.Component):
    i_stream: In(stream.Signature(8))
    read_i_stream: In(stream.Signature(8))
    o_stream: Out(stream.Signature(8))
    o_flush:  Out(1)

    enabled:        In(1)
    generation:     In(16)
    read_only:      In(1)
    prime_first_read: In(1)
    image_size:     In(IMAGE_SIZE_WIDTH)
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
    prefetch_wait_cycles: Out(32)
    max_prefetch_wait: Out(32)
    max_host_service_latency: Out(32)
    diagnostics_snapshot: Out(FPGA_DIAGNOSTICS_LAYOUT.size)

    def __init__(self, ports, *, max_sd_clock=25_000_000):
        self._ports = ports
        self._max_sd_clock = max_sd_clock
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
            platform.add_clock_constraint(clk_buffer.i, self._max_sd_clock)

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
            "prime_first_read": 1,
            "image_size": IMAGE_SIZE_WIDTH,
            "cid": 128,
            "csd": 128,
            "scr": 64,
        }))
        configuration_valid = Signal()
        with m.If(enabled_sd & ~configuration_valid):
            m.d.sd += [
                configuration.generation.eq(self.generation),
                configuration.read_only.eq(self.read_only),
                configuration.prime_first_read.eq(self.prime_first_read),
                configuration.image_size.eq(self.image_size),
                configuration.cid.eq(self.cid),
                configuration.csd.eq(self.csd),
                configuration.scr.eq(self.scr),
                configuration_valid.eq(1),
            ]
        engine_enabled = enabled_sd & configuration_valid

        m.submodules.host_decoder = host_decoder = COBSCRCFrameDecoder(FRAME_CRC16)
        wiring.connect(m, host_decoder.i, wiring.flipped(self.i_stream))

        m.submodules.host_reply_packer = host_reply_packer = HostReplyWordPacker()
        wiring.connect(m, host_decoder.o, host_reply_packer.i_stream)

        host_to_sd = AsyncQueue(
            shape=HOST_REPLY_WORD_LAYOUT,
            depth=16, i_domain="sync", o_domain="sd")
        m.submodules.raw_read_packer = raw_read_packer = RawBlockWordPacker()
        wiring.connect(m, wiring.flipped(self.read_i_stream), raw_read_packer.i_stream)
        raw_read_to_sd = AsyncQueue(
            shape=RAW_BLOCK_WORD_LAYOUT,
            # Two sequential packets may be outstanding while the host tops up the stream.
            # Four blocks of FIFO capacity leaves ample room for cancellation or seek handling.
            depth=256, i_domain="sync", o_domain="sd")
        sd_to_host = AsyncQueue(
            shape=data.StructLayout({"data": 8, "end": 1}),
            depth=64, i_domain="sd", o_domain="sync")
        m.submodules.host_to_sd = host_to_sd
        m.submodules.raw_read_to_sd = raw_read_to_sd
        m.submodules.sd_to_host = sd_to_host
        wiring.connect(m, host_to_sd.i, host_reply_packer.o_stream)
        wiring.connect(m, raw_read_to_sd.i, raw_read_packer.o_stream)

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
        diagnostics_snapshot = data.View(
            FPGA_DIAGNOSTICS_LAYOUT, self.diagnostics_snapshot)

        wiring.connect(m, command_engine.control, data_engine.control)
        wiring.connect(m, data_engine.storage, host_engine.storage)
        wiring.connect(m, host_engine.buffer, block_buffer.host)
        wiring.connect(m, data_engine.buffer, block_buffer.data)
        wiring.connect(m, host_engine.i_stream, host_to_sd.o)
        wiring.connect(m, host_engine.read_i_stream, raw_read_to_sd.o)
        wiring.connect(m, sd_to_host.i, host_engine.o_stream)

        m.d.comb += [
            command_engine.cmd_i.eq(cmd_buffer.i),
            cmd_buffer.o.eq(command_engine.cmd_o),
            cmd_buffer.oe.eq(command_engine.cmd_oe),
            command_engine.enabled.eq(engine_enabled & ~block_buffer.fault),
            command_engine.read_only.eq(configuration.read_only),
            command_engine.support_4bit.eq(configuration.scr[50]),
            command_engine.image_size.eq(configuration.image_size),
            command_engine.cid.eq(configuration.cid),
            command_engine.csd.eq(configuration.csd),

            data_engine.dat_i.eq(dat_i),
            data_engine.scr.eq(configuration.scr),
            data_engine.image_size.eq(configuration.image_size),
            data_engine.prime_first_read.eq(configuration.prime_first_read),

            host_engine.generation.eq(configuration.generation),
            host_engine.image_size.eq(configuration.image_size),
            host_engine.current_address.eq(command_engine.control.current_address),
            host_engine.erase_start.eq(command_engine.control.erase_start),
            host_engine.erase_end.eq(command_engine.control.erase_end),
            block_buffer.bank_states.eq(data_engine.read_bank_states),

            self.last_command.eq(command_engine.last_command),
            self.clock_count.eq(command_engine.clock_count),
            self.command_low_count.eq(command_engine.command_low_count),
            self.command_count.eq(command_engine.command_count),
            self.read_count.eq(data_engine.read_count),
            self.write_count.eq(data_engine.write_count),
            self.protocol_errors.eq(
                command_engine.protocol_errors + data_engine.protocol_errors +
                host_engine.protocol_errors + block_buffer.fault_count),
            self.backend_errors.eq(host_engine.backend_errors),
            self.stale_replies.eq(host_engine.stale_replies),
            self.buffer_faults.eq(block_buffer.fault_count),
            self.prefetch_waits.eq(data_engine.prefetch_waits),
            self.prefetch_wait_cycles.eq(data_engine.prefetch_wait_cycles),
            self.max_prefetch_wait.eq(data_engine.max_prefetch_wait),
            self.max_host_service_latency.eq(host_engine.max_host_service_latency),

            diagnostics_snapshot.sd_clocks.eq(command_engine.clock_count),
            diagnostics_snapshot.command_low_clocks.eq(
                command_engine.command_low_count),
            diagnostics_snapshot.commands.eq(command_engine.command_count),
            diagnostics_snapshot.reads.eq(data_engine.read_count),
            diagnostics_snapshot.writes.eq(data_engine.write_count),
            diagnostics_snapshot.protocol_errors.eq(
                command_engine.protocol_errors + data_engine.protocol_errors +
                host_engine.protocol_errors + block_buffer.fault_count),
            diagnostics_snapshot.backend_errors.eq(host_engine.backend_errors),
            diagnostics_snapshot.stale_replies.eq(host_engine.stale_replies),
            diagnostics_snapshot.buffer_faults.eq(block_buffer.fault_count),
            diagnostics_snapshot.prefetch_waits.eq(data_engine.prefetch_waits),
            diagnostics_snapshot.prefetch_wait_cycles.eq(
                data_engine.prefetch_wait_cycles),
            diagnostics_snapshot.max_prefetch_wait_cycles.eq(
                data_engine.max_prefetch_wait),
            diagnostics_snapshot.max_host_service_latency_cycles.eq(
                host_engine.max_host_service_latency),
            diagnostics_snapshot.card_state.eq(command_engine.control.card_state),
            diagnostics_snapshot.data_mode.eq(command_engine.control.data_mode),
            diagnostics_snapshot.read_bank_states.eq(data_engine.read_bank_states),
            diagnostics_snapshot.host_transaction_phase.eq(host_engine.transaction_phase),
            diagnostics_snapshot.host_reply_header_bytes.eq(host_engine.reply_header_bytes),
            diagnostics_snapshot.host_reply_payload_bytes.eq(host_engine.reply_payload_bytes),
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
            max_sd_clock: int = DEFAULT_TRANSFER_SPEED,
            worker_factory=BackendWorker):
        self._logger = logger
        self._level = logging.DEBUG if self._logger.name == __name__ else logging.TRACE

        ports = assembly.add_port_group(clk=clk, cmd=cmd, dat=dat)
        assembly.use_pulls({cmd: "high", dat: "high"})
        component = assembly.add_submodule(
            SDCardComponent(ports, max_sd_clock=max_sd_clock))
        self._pipe = assembly.add_inout_pipe(
            component.o_stream, component.i_stream, in_flush=component.o_flush,
            in_fifo_depth=0, out_fifo_depth=0,
            in_buffer_size=4096, out_buffer_size=4096)
        self._read_pipe = assembly.add_out_pipe(
            component.read_i_stream, fifo_depth=0, buffer_size=4096)

        self._enabled = assembly.add_rw_register(component.enabled)
        self._generation_reg = assembly.add_rw_register(component.generation)
        self._read_only_reg = assembly.add_rw_register(component.read_only)
        self._prime_first_read_reg = assembly.add_rw_register(component.prime_first_read)
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
        self.prefetch_wait_cycles = assembly.add_ro_register(component.prefetch_wait_cycles)
        self.max_prefetch_wait = assembly.add_ro_register(component.max_prefetch_wait)
        self.max_host_service_latency = assembly.add_ro_register(
            component.max_host_service_latency)
        self._diagnostics_snapshot = assembly.add_ro_register(component.diagnostics_snapshot)

        self._session = None
        self._worker_factory = worker_factory
        self._lifecycle_lock = asyncio.Lock()
        self._operation_lock = asyncio.Lock()
        self._generation = 0
        self._active = False
        self._flush_count = 0
        self._host_backend_errors = 0

    @property
    def backend(self) -> BlockBackend | None:
        if self._session is None:
            return None
        return self._session.backend

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
            transfer_speed: int = DEFAULT_TRANSFER_SPEED,
            support_4bit: bool = True,
            prime_first_read: bool = False):
        if (image_path is None) == (backend is None):
            raise ValueError("exactly one of image_path or backend must be provided")
        if backend is None:
            backend = FileBlockBackend(image_path, read_only=read_only)
        worker = self._worker_factory(backend)
        new_session = _BackendSession(backend, worker, sync_writes)
        try:
            size = backend.size
            encoded_transfer_speed = encode_csd_transfer_speed(transfer_speed)
            advertised_transfer_speed = decode_csd_transfer_speed(encoded_transfer_speed)
            csd = build_csd(
                size, read_only=backend.read_only, transfer_speed=transfer_speed)
            cid = build_cid(serial=backend.identity)
            scr = build_scr(support_4bit=support_4bit)
            if isinstance(backend, FileBlockBackend):
                # Prime the bounded host read-ahead cache before the FPGA can issue its first
                # request. The Sharp PXA25x driver can time out its FIFO before an uncached
                # worker-thread plus USB round trip completes; a cache hit keeps that first
                # response on the non-blocking path and also covers a contiguous boot kernel.
                await worker.read_block(0)
        except BaseException as primary_error:
            try:
                await worker.close()
            except BaseException as close_error:
                raise BaseExceptionGroup(
                    "SD backend configuration and cleanup both failed",
                    [primary_error, close_error]) from None
            raise

        async with self._lifecycle_lock:
            async with self._operation_lock:
                adopted = False
                try:
                    await self._enabled.set(0)
                    self._active = False
                    await self._pipe.reset()
                    await self._read_pipe.reset()
                    old_session = self._session
                    self._session = None
                    if old_session is not None:
                        await self._retire_session(old_session, operation="reconfigure")

                    generation = self._next_generation()
                    await self._generation_reg.set(generation)
                    await self._read_only_reg.set(backend.read_only)
                    await self._prime_first_read_reg.set(prime_first_read)
                    await self._image_size.set(size)
                    await self._cid.set(cid)
                    await self._csd.set(csd)
                    await self._scr.set(scr)
                    self._flush_count = 0
                    self._host_backend_errors = 0
                    self._session = new_session
                    adopted = True
                    await self._enabled.set(1)
                    self._active = True
                except BaseException as primary_error:
                    cleanup_errors = []
                    try:
                        await self._enabled.set(0)
                    except BaseException as error:
                        cleanup_errors.append(error)
                    self._active = False
                    if adopted:
                        self._session = None
                    try:
                        await worker.close()
                    except BaseException as close_error:
                        cleanup_errors.append(close_error)
                    if cleanup_errors:
                        raise BaseExceptionGroup(
                            "SD backend activation and cleanup both failed",
                            [primary_error, *cleanup_errors]) from None
                    raise

        access = "read-only" if backend.read_only else "writable"
        durability = "synchronous" if sync_writes else "write-back"
        self._logger.info(
            "serving %s as a %.2f MiB SDSC image (%s, %s, %.3g Mbit/s CSD%s)",
            getattr(backend, "path", backend.__class__.__name__), size / 2**20,
            access, durability, advertised_transfer_speed / 1_000_000,
            ("" if advertised_transfer_speed == transfer_speed else
             f", requested {transfer_speed / 1_000_000:.3g} Mbit/s"))

    async def _retire_session(self, session, *, operation):
        errors = []
        try:
            await session.worker.flush()
            self._flush_count += 1
        except BaseException as error:
            if isinstance(error, BackendError):
                self._record_backend_error(f"{operation} flush", error)
            errors.append(error)
        try:
            await session.worker.close()
        except BaseException as error:
            errors.append(error)
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup(
                f"SD backend {operation} failed during flush and close", errors)

    async def suspend(self, *, reason: str = "requested"):
        async with self._lifecycle_lock:
            async with self._operation_lock:
                if not self._active:
                    return
                self._logger.info("suspending SD card: %s", reason)
                # Mark the interface inactive before disabling the FPGA. The register write can
                # wake a blocked pipe receive; serve() must see that the resulting transport
                # exception belongs to a deliberate suspend and retain the backend for resume.
                self._active = False
                await self._enabled.set(0)
                await self._generation_reg.set(self._next_generation())
                await self._pipe.reset()
                await self._read_pipe.reset()

    async def resume(self, *, reason: str = "requested"):
        async with self._lifecycle_lock:
            async with self._operation_lock:
                if self._session is None or self._active:
                    return
                await self._pipe.reset()
                await self._read_pipe.reset()
                await self._generation_reg.set(self._next_generation())
                await self._enabled.set(1)
                self._active = True
                self._logger.info(
                    "resumed SD card: %s (generation %#06x)", reason, self._generation)

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
        async with self._lifecycle_lock:
            async with self._operation_lock:
                session = self._session
                diagnostics = None
                errors = []
                if session is not None:
                    # Disabling the card asynchronously resets the SD domain and its counters, so
                    # capture FPGA fields first. Host fields are finalized after shutdown.
                    try:
                        diagnostics = await self._read_diagnostics_unlocked(session)
                    except BaseException as error:
                        errors.append(error)
                try:
                    await self._enabled.set(0)
                except BaseException as error:
                    errors.append(error)
                self._active = False
                if session is None:
                    self._raise_lifecycle_errors("SD card shutdown failed", errors)
                    return

                self._session = None
                try:
                    await self._retire_session(session, operation="close")
                except BaseException as error:
                    errors.append(error)
                if diagnostics is not None:
                    diagnostics = replace(
                        diagnostics,
                        host_backend_errors=self._host_backend_errors,
                        durable_flushes=self._flush_count,
                        backend_calls=getattr(session.worker, "service_count", 0),
                        max_backend_call_seconds=getattr(
                            session.worker, "max_service_latency", 0.0),
                    )
                    self._log_diagnostics(diagnostics)
                self._raise_lifecycle_errors("SD card shutdown failed", errors)

    @staticmethod
    def _raise_lifecycle_errors(message, errors):
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup(message, errors)

    def _log_diagnostics(self, diagnostics):
        self._logger.info(
            "stopped after %d SD clocks (%d CMD-low), %d commands, %d reads, %d writes, "
            "%d protocol errors, %d FPGA-reported backend errors, %d stale replies; "
            "%d buffer faults, %d prefetch waits (%d total, %d max SD clocks), "
            "%d max host-service SD clocks; %d host backend errors, %d durable flushes, "
            "%d backend calls, %.3f ms max backend call; state card=%d data=%d banks=%#x "
            "host-phase=%d reply=%d+%d bytes",
            diagnostics.sd_clocks, diagnostics.command_low_clocks,
            diagnostics.commands, diagnostics.reads, diagnostics.writes,
            diagnostics.protocol_errors, diagnostics.fpga_backend_errors,
            diagnostics.stale_replies, diagnostics.buffer_faults,
            diagnostics.prefetch_waits, diagnostics.prefetch_wait_cycles,
            diagnostics.max_prefetch_wait_cycles,
            diagnostics.max_host_service_latency_cycles,
            diagnostics.host_backend_errors, diagnostics.durable_flushes,
            diagnostics.backend_calls, diagnostics.max_backend_call_seconds * 1e3,
            diagnostics.card_state, diagnostics.data_mode, diagnostics.read_bank_states,
            diagnostics.host_transaction_phase, diagnostics.host_reply_header_bytes,
            diagnostics.host_reply_payload_bytes)

    async def read_diagnostics(self) -> SDCardDiagnostics:
        """Read named FPGA and host-side pipeline diagnostics.

        The FPGA fields are latched by one packed register transaction, so they represent one
        instant even during an active transfer. The close-time report samples that register
        immediately before disabling the SD clock domain, then updates host fields after the
        final flush and worker close.
        """
        async with self._lifecycle_lock:
            async with self._operation_lock:
                return await self._read_diagnostics_unlocked(self._session)

    async def _read_diagnostics_unlocked(self, session) -> SDCardDiagnostics:
        worker = None if session is None else session.worker
        fpga = decode_fpga_diagnostics(await self._diagnostics_snapshot)
        return SDCardDiagnostics(
            sd_clocks=fpga["sd_clocks"],
            command_low_clocks=fpga["command_low_clocks"],
            commands=fpga["commands"],
            reads=fpga["reads"],
            writes=fpga["writes"],
            protocol_errors=fpga["protocol_errors"],
            fpga_backend_errors=fpga["backend_errors"],
            stale_replies=fpga["stale_replies"],
            buffer_faults=fpga["buffer_faults"],
            prefetch_waits=fpga["prefetch_waits"],
            prefetch_wait_cycles=fpga["prefetch_wait_cycles"],
            max_prefetch_wait_cycles=fpga["max_prefetch_wait_cycles"],
            max_host_service_latency_cycles=fpga["max_host_service_latency_cycles"],
            host_backend_errors=self._host_backend_errors,
            durable_flushes=self._flush_count,
            backend_calls=getattr(worker, "service_count", 0),
            max_backend_call_seconds=getattr(worker, "max_service_latency", 0.0),
            card_state=fpga["card_state"],
            data_mode=fpga["data_mode"],
            read_bank_states=fpga["read_bank_states"],
            host_transaction_phase=fpga["host_transaction_phase"],
            host_reply_header_bytes=fpga["host_reply_header_bytes"],
            host_reply_payload_bytes=fpga["host_reply_payload_bytes"],
        )

    def _record_backend_error(self, operation: str, error: BackendError):
        self._host_backend_errors += 1
        self._logger.error("host-backed SD %s failed (%s): %s",
            operation, error.status.name, error)

    async def flush(self):
        async with self._lifecycle_lock:
            async with self._operation_lock:
                if self._session is None:
                    raise SDCardError("no image is configured")
                await self._flush_session(self._session)

    async def _flush_session(self, session):
        await session.worker.flush()
        self._flush_count += 1

    async def _reply(self, opcode: HostReply, generation: int, request_id: int,
            status: BackendStatus = BackendStatus.OK, payload=b""):
        await self._reply_many(((opcode, generation, request_id, status, payload),))

    async def _reply_many(self, replies):
        packets = b"".join(
            ReplyFrame.create(opcode, generation, request_id, status, payload).encode_packet()
            for opcode, generation, request_id, status, payload in replies)
        await self._pipe.send(packets)
        await self._pipe.flush()

    async def serve_once(self):
        if self._session is None:
            raise SDCardError("no image is configured")

        while True:
            packet = await self._pipe.recv_until(b"\0")
            try:
                request = RequestFrame.decode_packet(packet)
            except FrameError as error:
                self._logger.warning("discarding malformed SD backend frame: %s", error)
                continue
            break

        async with self._operation_lock:
            session = self._session
            if session is None:
                return
            await self._serve_request(request, session)

    async def _serve_request(self, request, session):
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
            if header.opcode == HostEvent.Read:
                await self._read_pipe.send(bytes([0xff]) * BLOCK_SIZE)
                await self._read_pipe.flush()
            else:
                await self._reply(
                    operation.reply, header.generation, header.request_id,
                    BackendStatus.Invalid, operation.invalid_reply_payload())
            return

        if header.opcode == HostEvent.Read:
            address = fields["address"]
            blocks = fields["blocks"]
            if blocks not in (1, 2):
                await self._read_pipe.send(bytes([0xff]) * BLOCK_SIZE)
                await self._read_pipe.flush()
                return
            raw_blocks = []
            for index in range(blocks):
                block_address = address + index * BLOCK_SIZE
                self._logger.log(self._level, "read block at %#x", block_address)
                status = BackendStatus.OK
                try:
                    block = await session.worker.read_block(block_address)
                except BackendError as error:
                    self._record_backend_error("read", error)
                    status = error.status
                    block = bytes([0xff]) * BLOCK_SIZE
                raw_blocks.append(block)
            # One unframed 512-byte block maps to one full USB high-speed packet. USB already
            # supplies link CRC and the FPGA accepts it only for the sole outstanding tagged read
            # transaction; avoiding a short second packet is essential for the PXA25x timer.
            await self._read_pipe.send(b"".join(raw_blocks))
            await self._read_pipe.flush()

        elif header.opcode == HostEvent.Write:
            address = fields["address"]
            block = fields["data"]
            self._logger.log(self._level, "write block at %#x", address)
            status = BackendStatus.OK
            try:
                await session.worker.write_block(address, block)
                if session.sync_writes:
                    await self._flush_session(session)
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
                await session.worker.erase(start, end)
                if session.sync_writes:
                    await self._flush_session(session)
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
                if not self._active:
                    await asyncio.sleep(0.05)
                    continue
                try:
                    await self.serve_once()
                except asyncio.CancelledError:
                    # Pipe reset cancels the pending receive future without cancelling this task.
                    # Preserve ordinary task cancellation so shutdown still reaches close().
                    task = asyncio.current_task()
                    if (self._session is not None and not self._active and
                            task is not None and task.cancelling() == 0):
                        continue
                    raise
                except Exception:
                    # A deliberate target-power suspend resets the transport to
                    # discard partial frames. Keep serving the retained backend
                    # after resume instead of treating that reset as shutdown.
                    if self._session is not None and not self._active:
                        continue
                    raise
        finally:
            await self.close()


class SDCardApplet(GlasgowAppletV2):
    logger = logging.getLogger(__name__)
    help = "emulate a host-backed SD memory card"
    description = """
    Emulate a byte-addressed SDSC memory card using a disk image on the host.

    The FPGA stores two tagged 512-byte data blocks and bounded transport FIFOs. Read blocks
    are fetched from the host during the card access delay, and write blocks keep DAT0 busy until
    the host has written and synchronized them to the image. Target-power monitoring automatically
    resets protocol and transport state when a device such as a Zaurus removes and restores its
    SD rail. Images must be 2 KiB aligned and may be as large as 1 GiB, the maximum representable
    by this CSD v1 profile.

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
        parser.add_argument("--max-sd-clock", type=parse_transfer_speed,
            default=DEFAULT_TRANSFER_SPEED, metavar="RATE",
            help="constrain gateware for the maximum host clock (default: 25m)")

    def build(self, args):
        with self.assembly.add_applet(self):
            self.assembly.use_voltage(args.voltage)
            self.sd_card_iface = SDCardInterface(self.logger, self.assembly,
                clk=args.clk, cmd=args.cmd, dat=args.dat,
                max_sd_clock=args.max_sd_clock)
            self._sd_port = args.clk.port

    @classmethod
    def add_setup_arguments(cls, parser):
        parser.add_argument("image", metavar="IMAGE",
            help="serve IMAGE as a writable SDSC card (2 KiB aligned, at most 1 GiB)")
        parser.add_argument("--read-only", action="store_true",
            help="advertise write protection and open IMAGE read-only")
        parser.add_argument("--volatile-writes", action="store_true",
            help="acknowledge writes before fsync (the image is still flushed on exit)")
        parser.add_argument("--transfer-speed", type=parse_transfer_speed,
            default=DEFAULT_TRANSFER_SPEED, metavar="RATE",
            help="advertise a CSD transfer speed such as 25m or 5m (default: 25m)")
        parser.add_argument("--one-bit", action="store_true",
            help="advertise only the 1-bit bus for timing-sensitive legacy hosts")
        parser.add_argument("--prime-first-read", action="store_true",
            help="queue sectors 0 and 1 for a latency-sensitive initial two-block read")
        parser.add_argument("--no-power-monitor", action="store_true",
            help="keep the card and port Vio active while the target rail changes")
        parser.add_argument("--power-off-threshold", type=float, default=1.0, metavar="V",
            help="treat the target rail as off below V (default: 1.0)")
        parser.add_argument("--power-on-threshold", type=float, default=1.8, metavar="V",
            help="treat the target rail as on above V (default: 1.8)")
        parser.add_argument("--power-poll-interval", type=float, default=0.25, metavar="SECONDS",
            help="target-power polling interval (default: 0.25)")
        parser.add_argument("--power-debounce-samples", type=int, default=3, metavar="COUNT",
            help="consecutive samples required for a power transition (default: 3)")
        parser.add_argument("--diagnostics-interval", type=float, default=0.0,
            metavar="SECONDS",
            help="periodically log FPGA and backend diagnostics (default: disabled)")

    async def setup(self, args):
        if args.no_power_monitor:
            # mirror_voltage() normally protects the target by installing a tight Vsense
            # alert. A Zaurus deliberately removes the SD rail during reboot; that alert
            # permanently disables Glasgow's Vio regulator and leaves the bidirectional CMD
            # transceiver unpowered when the rail returns. In this explicit mode the breakout's
            # Vio output must remain isolated from the target supply, and the FPGA card stays
            # ready to receive MAINTE's first command without a host-side polling round trip.
            await self.device.reset_alert(str(self._sd_port))
            await self.device.poll_alert()
            self.logger.warning(
                "target-power protection disabled on port %s; keep Glasgow Vio isolated "
                "from the target supply", self._sd_port)
        try:
            await self.sd_card_iface.configure(
                args.image, read_only=args.read_only, sync_writes=not args.volatile_writes,
                transfer_speed=args.transfer_speed, support_4bit=not args.one_bit,
                prime_first_read=args.prime_first_read)
        except OSError as error:
            raise SDCardError(f"cannot open image {args.image!r}: {error.strerror}") from error

    async def run(self, args):
        async def log_diagnostics():
            while True:
                await asyncio.sleep(args.diagnostics_interval)
                self.sd_card_iface._log_diagnostics(
                    await self.sd_card_iface.read_diagnostics())

        if args.no_power_monitor and args.diagnostics_interval <= 0:
            await self.sd_card_iface.serve()
            return
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(self.sd_card_iface.serve())
            if not args.no_power_monitor:
                tasks.create_task(self.sd_card_iface.monitor_power(
                    self.device, self._sd_port,
                    off_threshold=args.power_off_threshold,
                    on_threshold=args.power_on_threshold,
                    interval=args.power_poll_interval,
                    debounce_samples=args.power_debounce_samples))
            if args.diagnostics_interval > 0:
                tasks.create_task(log_diagnostics())

    @classmethod
    def tests(cls):
        from . import test
        return test.SDCardAppletTestCase
