import asyncio
from dataclasses import dataclass, replace
import logging
from pathlib import Path
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
    read_only: bool = False
    prime_first_read: bool = False
    image_size: int = 0
    cid: int = 0
    csd: int = 0
    scr: int = 0


def _read_boot_blocks(image_path):
    """Read the MBR and first partition boot block used by legacy loaders."""
    try:
        with image_path.open("rb") as image:
            mbr = image.read(BLOCK_SIZE)
            if len(mbr) != BLOCK_SIZE:
                raise SDCardError(
                    f"boot-prefix image must contain at least {BLOCK_SIZE * 2} bytes")

            partition_lba = 1
            if mbr[510:512] == b"\x55\xaa":
                for index in range(4):
                    entry = mbr[446 + index * 16:446 + (index + 1) * 16]
                    candidate_lba = int.from_bytes(entry[8:12], "little")
                    sector_count = int.from_bytes(entry[12:16], "little")
                    if entry[4] != 0 and candidate_lba != 0 and sector_count != 0:
                        partition_lba = candidate_lba
                        break

            boot_address = partition_lba * BLOCK_SIZE
            image.seek(boot_address)
            partition_boot = image.read(BLOCK_SIZE)
            if len(partition_boot) != BLOCK_SIZE:
                raise SDCardError(
                    f"boot-prefix image does not contain partition boot sector "
                    f"at LBA {partition_lba}")
    except OSError as error:
        raise SDCardError(
            f"cannot read boot prefix {str(image_path)!r}: {error.strerror}") from error

    return mbr + partition_boot, (0, boot_address)


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

    def __init__(self, ports, *, max_sd_clock=25_000_000, boot_data=None,
            boot_addresses=None, capture_cmd17_wire=False):
        self._ports = ports
        self._max_sd_clock = max_sd_clock
        self._boot_data = boot_data
        self._capture_cmd17_wire = capture_cmd17_wire
        self._boot_addresses = (
            boot_addresses if boot_addresses is not None
            else (0, BLOCK_SIZE))
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

        # Unlike the SD engines, the host transport runs from Glasgow's always-on sync clock.
        # Reset its sync-side state while the card is disabled so a target power cycle cannot
        # splice a partial old COBS frame or raw block into the first request of the new session.
        # Holding enabled low over the sensed power-off interval guarantees this synchronous
        # reset is observed; no target-provided SD clocks are required.
        transport_reset = ~self.enabled
        host_decoder = COBSCRCFrameDecoder(FRAME_CRC16)
        m.submodules.host_decoder = ResetInserter(transport_reset)(host_decoder)
        wiring.connect(m, host_decoder.i, wiring.flipped(self.i_stream))

        host_reply_packer = HostReplyWordPacker()
        m.submodules.host_reply_packer = ResetInserter(transport_reset)(host_reply_packer)
        wiring.connect(m, host_decoder.o, host_reply_packer.i_stream)

        host_to_sd = AsyncQueue(
            shape=HOST_REPLY_WORD_LAYOUT,
            depth=16, i_domain="sync", o_domain="sd")
        raw_read_packer = RawBlockWordPacker()
        m.submodules.raw_read_packer = ResetInserter(transport_reset)(raw_read_packer)
        m.d.comb += raw_read_packer.generation.eq(self.generation)
        wiring.connect(m, wiring.flipped(self.read_i_stream), raw_read_packer.i_stream)
        raw_read_to_sd = AsyncQueue(
            shape=RAW_BLOCK_WORD_LAYOUT,
            # Two sequential packets may be outstanding while the host tops up the stream.
            # Four blocks of FIFO capacity leaves ample room for cancellation or seek handling.
            depth=256, i_domain="sync", o_domain="sd")
        sd_to_host = AsyncQueue(
            shape=data.StructLayout({"data": 8, "end": 1}),
            depth=64, i_domain="sd", o_domain="sync")
        m.submodules.host_to_sd = ResetInserter({"sync": transport_reset})(host_to_sd)
        m.submodules.raw_read_to_sd = ResetInserter({"sync": transport_reset})(raw_read_to_sd)
        m.submodules.sd_to_host = ResetInserter({"sync": transport_reset})(sd_to_host)
        wiring.connect(m, host_to_sd.i, host_reply_packer.o_stream)
        wiring.connect(m, raw_read_to_sd.i, raw_read_packer.o_stream)

        host_encoder = COBSCRCFrameEncoder(FRAME_CRC16, fifo_depth=1024)
        m.submodules.host_encoder = ResetInserter(transport_reset)(host_encoder)
        wiring.connect(m, host_encoder.i, sd_to_host.o)
        wiring.connect(m, wiring.flipped(self.o_stream), host_encoder.o)
        m.d.sync += self.o_flush.eq(0)
        with m.If(self.o_stream.valid & self.o_stream.ready & (self.o_stream.payload == 0)):
            m.d.sync += self.o_flush.eq(1)

        m.submodules.block_buffer = block_buffer = BlockBuffer(boot_data=self._boot_data)

        dat_i = Cat(*(buffer.i for buffer in dat_buffers))
        m.submodules.command_engine = command_engine = CommandEngine(
            capture_cmd17_wire=self._capture_cmd17_wire)
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
            data_engine.boot_preinitialized.eq(self._boot_data is not None),
            data_engine.boot_address_0.eq(self._boot_addresses[0]),
            data_engine.boot_address_1.eq(self._boot_addresses[1]),
            data_engine.boot_restored.eq(block_buffer.boot_restored),
            block_buffer.restore_boot.eq(data_engine.restore_boot),

            host_engine.generation.eq(configuration.generation),
            host_engine.image_size.eq(configuration.image_size),
            host_engine.current_address.eq(command_engine.control.current_address),
            host_engine.erase_start.eq(command_engine.control.erase_start),
            host_engine.erase_end.eq(command_engine.control.erase_end),
            host_engine.buffer_write_blocked.eq(data_engine.buffer.write_en.any()),
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
            static_pulls: bool = True, boot_data=None, boot_addresses=None,
            capture_cmd17_wire=False,
            worker_factory=BackendWorker):
        self._logger = logger
        self._level = logging.DEBUG if self._logger.name == __name__ else logging.TRACE

        ports = assembly.add_port_group(clk=clk, cmd=cmd, dat=dat)
        self._clk_pin_number = clk.number
        self._pull_pin_numbers = {cmd.number, *(pin.number for pin in dat)}
        if static_pulls:
            assembly.use_pulls({cmd: "high", dat: "high"})
        component = assembly.add_submodule(
            SDCardComponent(
                ports, max_sd_clock=max_sd_clock, boot_data=boot_data,
                boot_addresses=boot_addresses,
                capture_cmd17_wire=capture_cmd17_wire))
        self._boot_data = boot_data
        self._boot_addresses = (
            boot_addresses if boot_data is not None else None)
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
        self._transport_epoch = 0
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

    async def _reset_transport(self):
        # A service coroutine may still be blocked in recv_until() while the USB pipes are
        # reset. Record the reset before touching either pipe so serve() can distinguish the
        # resulting cancellation/error from an actual applet failure even if the card has
        # already been re-enabled by the time the exception is delivered.
        self._transport_epoch += 1
        await self._pipe.reset()
        await self._read_pipe.reset()

    async def _write_card_configuration(self, session, generation):
        # Hardware pipe reset asserts the complete applet clock-domain reset. The RW registers
        # live in that same domain, so resetting either transport also clears every card setting,
        # not only the pipe state. Always restore the coherent retained configuration after the
        # reset and before enabling the SD clock domain.
        await self._generation_reg.set(generation)
        await self._read_only_reg.set(session.read_only)
        await self._prime_first_read_reg.set(session.prime_first_read)
        await self._image_size.set(session.image_size)
        await self._cid.set(session.cid)
        await self._csd.set(session.csd)
        await self._scr.set(session.scr)

    async def configure(self, image_path: str | None = None, *,
            backend: BlockBackend | None = None, read_only: bool = False,
            sync_writes: bool = True,
            transfer_speed: int = DEFAULT_TRANSFER_SPEED,
            support_4bit: bool = True,
            prime_first_read: bool = False, activate: bool = True):
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
            new_session = replace(
                new_session,
                read_only=backend.read_only,
                prime_first_read=prime_first_read,
                image_size=size,
                cid=cid,
                csd=csd,
                scr=scr)
            if self._boot_data is not None:
                actual_boot_data = b"".join(
                    [await worker.read_block(address)
                     for address in self._boot_addresses])
                if actual_boot_data != self._boot_data:
                    raise SDCardError(
                        "the image boot blocks do not match the data embedded "
                        "in this bitstream")
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
                    await self._reset_transport()
                    old_session = self._session
                    self._session = None
                    if old_session is not None:
                        await self._retire_session(old_session, operation="reconfigure")

                    generation = self._next_generation()
                    await self._write_card_configuration(new_session, generation)
                    self._flush_count = 0
                    self._host_backend_errors = 0
                    self._session = new_session
                    adopted = True
                    await self._enabled.set(int(activate))
                    self._active = activate
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

    async def set_signal_pulls(self, device, port, *, enabled: bool):
        high = self._pull_pin_numbers if enabled else set()
        # An armed engine must not see a floating clock while the target is unpowered: random
        # crossings would advance and corrupt its SD state before the real host starts. A weak
        # pull-down cannot back-power the target and the powered host actively overrides it.
        await device.set_pulls(str(port), low={self._clk_pin_number}, high=high)
        self._logger.info(
            "port %s SD pulls set to CLK-low%s",
            port, ", CMD/DAT-high" if enabled else "")

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

    async def suspend(self, *, reason: str = "requested", after_disable=None):
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
                if after_disable is not None:
                    await after_disable()
                await self._generation_reg.set(self._next_generation())
                await self._reset_transport()

    async def resume(self, *, reason: str = "requested"):
        async with self._lifecycle_lock:
            async with self._operation_lock:
                if self._session is None or self._active:
                    return
                await self._reset_transport()
                await self._write_card_configuration(
                    self._session, self._next_generation())
                await self._enabled.set(1)
                self._active = True
                self._logger.info(
                    "resumed SD card: %s (generation %#06x)", reason, self._generation)

    async def rearm(self, *, reason: str = "requested", reset_hold: float = 0.05,
            after_disable=None):
        """Reset and re-enable the card while retaining its backing session."""
        if reset_hold < 0:
            raise SDCardError("reset hold time must be nonnegative")
        async with self._lifecycle_lock:
            async with self._operation_lock:
                if self._session is None:
                    return
                self._logger.info("rearming SD card: %s", reason)
                self._active = False
                await self._enabled.set(0)
                if after_disable is not None:
                    await after_disable()
                # Clear both USB endpoints as soon as the engine is disabled. A transfer that
                # was already submitted to the host controller can complete after cancellation,
                # so a timed rearm uses the hold as a quarantine interval and clears the pipes
                # again before assigning the new generation. This prevents a late old-session
                # raw block from becoming the first payload of the fresh card session.
                await self._reset_transport()
                if reset_hold > 0:
                    # Glasgow's transport and target-clock domains have different reset
                    # mechanisms. Keep enabled low for a real interval so the always-running
                    # sync side certainly resets even when external SD CLK has stopped, then
                    # discard any USB transfer that retired during that interval.
                    await asyncio.sleep(reset_hold)
                    await self._reset_transport()
                await self._write_card_configuration(
                    self._session, self._next_generation())
                await self._enabled.set(1)
                self._active = True
                self._logger.info(
                    "rearmed SD card: %s (generation %#06x)", reason, self._generation)

    async def monitor_power(self, device, port, *, off_threshold: float = 1.0,
            on_threshold: float = 1.8, interval: float = 0.25, debounce_samples: int = 3,
            manage_signal_pulls: bool = False, resume_settle: float = 0.0,
            rearm_min_interval: float = 0.0, rearm_reset_hold: float = 0.05):
        if rearm_min_interval < 0:
            raise SDCardError("power rearm minimum interval must be nonnegative")
        if rearm_reset_hold < 0:
            raise SDCardError("power rearm reset hold must be nonnegative")

        async def flush_after_suspend():
            try:
                await self.flush()
            except BackendError as error:
                self._record_backend_error("power-loss flush", error)

        async def suspend_for_power_loss(voltage):
            nonlocal target_power_active, last_rearm
            target_power_active = False
            if manage_signal_pulls:
                # Remove every sourcing pull, reset all protocol and transport state for a
                # guaranteed interval, then leave the fresh engine listening while the target
                # remains off. On this Zaurus the SD rail does not rise until ON, so waiting for
                # that edge to re-enable loses the beginning of MAINTE initialization.
                await self.set_signal_pulls(device, port, enabled=False)
                now = asyncio.get_running_loop().time()
                if now - last_rearm >= rearm_min_interval:
                    await self.rearm(
                        reason=f"target rail fell to {voltage:.2f} V",
                        reset_hold=rearm_reset_hold)
                    last_rearm = asyncio.get_running_loop().time()
                    self._logger.info(
                        "target rail fell to %.2f V; fresh SD engine rearmed", voltage)
                else:
                    self._logger.info(
                        "target rail fell to %.2f V only %.3f s after rearm; "
                        "kept fresh engine armed through boot-time rail chatter",
                        voltage, now - last_rearm)
            else:
                await self.suspend(reason=f"target rail fell to {voltage:.2f} V")

        async def resume_after_power_restore(voltage):
            nonlocal target_power_active, last_rearm
            if manage_signal_pulls:
                await self.set_signal_pulls(device, port, enabled=True)
                if resume_settle > 0:
                    await asyncio.sleep(resume_settle)
                if not self._active:
                    await self.resume(reason=f"target rail rose to {voltage:.2f} V")
                # Anchor chatter suppression to every real rise. The target may remain off for
                # hours after rearming, while its characteristic second dip follows ON closely.
                last_rearm = asyncio.get_running_loop().time()
                target_power_active = True
                self._logger.info(
                    "target rail rose to %.2f V; fresh SD card is listening", voltage)
            else:
                await self.resume(reason=f"target rail rose to {voltage:.2f} V")

        target_power_active = False
        last_rearm = float("-inf")
        try:
            if manage_signal_pulls:
                # Establish a safe initial state before entering the edge-oriented debouncer.
                # The applet may be started with either an already-powered target or a fully
                # unpowered one. In the latter case configure() has briefly enabled the engine,
                # so perform a complete timed rearm now and leave the fresh card listening.
                initial_voltage = await device.measure_voltage(str(port))
                if initial_voltage > on_threshold:
                    await self.set_signal_pulls(device, port, enabled=True)
                    target_power_active = True
                    self._logger.info(
                        "target rail initially active at %.2f V; SD card listening",
                        initial_voltage)
                else:
                    await self.set_signal_pulls(device, port, enabled=False)
                    await self.rearm(
                        reason=f"target rail initially at {initial_voltage:.2f} V",
                        reset_hold=rearm_reset_hold)
                    await flush_after_suspend()
                    last_rearm = asyncio.get_running_loop().time()
                    self._logger.info(
                        "target rail initially at %.2f V; fresh SD engine armed before ON",
                        initial_voltage)

            monitor = TargetPowerLifecycle(
                self._logger,
                sample_voltage=lambda: device.measure_voltage(str(port)),
                is_active=(
                    (lambda: target_power_active) if manage_signal_pulls
                    else (lambda: self._active)),
                after_suspend=flush_after_suspend,
                suspend=suspend_for_power_loss,
                resume=resume_after_power_restore,
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
            if raw_blocks:
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
                transport_epoch = self._transport_epoch
                try:
                    await self.serve_once()
                except asyncio.CancelledError:
                    # Pipe reset cancels the pending receive future without cancelling this task.
                    # Preserve ordinary task cancellation so shutdown still reaches close().
                    task = asyncio.current_task()
                    if (self._session is not None and
                            (not self._active or transport_epoch != self._transport_epoch) and
                            task is not None and task.cancelling() == 0):
                        continue
                    raise
                except Exception:
                    # A deliberate lifecycle transition resets the transport to discard partial
                    # frames. It may immediately rearm, so use the reset epoch rather than only
                    # the current active flag to recognize and retain the backend session.
                    if (self._session is not None and
                            (not self._active or transport_epoch != self._transport_epoch)):
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
    the host has written and synchronized them to the image. Target-power monitoring handles
    devices such as a Zaurus that remove and restore their SD rail. Images must be 2 KiB aligned
    and may be as large as 1 GiB, the maximum representable by this CSD v1 profile.

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
        parser.add_argument("--boot-prefix", type=Path, metavar="IMAGE",
            help="embed IMAGE MBR and first partition boot sector for deterministic startup")
        parser.add_argument("--capture-cmd17-wire", action="store_true",
            help="diagnostic build: capture the pad-observed CMD17 R1 in clock counters")
        power_idle_group = parser.add_mutually_exclusive_group()
        power_idle_group.add_argument(
            "--power-idle-high-z", dest="power_idle_high_z", action="store_true",
            default=None,
            help="keep fixed Vio on but remove sourcing pulls while target SD power is absent "
                 "(default with fixed nonzero Vio and power monitoring)")
        power_idle_group.add_argument(
            "--no-power-idle-high-z", dest="power_idle_high_z", action="store_false",
            help="reset the card engine while target SD power is absent")

    @staticmethod
    def _resolve_power_idle_high_z(args):
        if args.power_idle_high_z is not None:
            return args.power_idle_high_z
        if getattr(args, "no_power_monitor", False):
            return False
        vio = args.voltage.get(args.clk.port)
        return vio is not None and vio.value is not None and vio.value > 0

    def build(self, args):
        args.power_idle_high_z = self._resolve_power_idle_high_z(args)
        boot_data = None
        boot_addresses = None
        if args.boot_prefix is not None:
            boot_data, boot_addresses = _read_boot_blocks(args.boot_prefix)
        with self.assembly.add_applet(self):
            self.assembly.use_voltage(args.voltage)
            self.sd_card_iface = SDCardInterface(self.logger, self.assembly,
                clk=args.clk, cmd=args.cmd, dat=args.dat,
                max_sd_clock=args.max_sd_clock,
                boot_data=boot_data,
                boot_addresses=boot_addresses,
                capture_cmd17_wire=args.capture_cmd17_wire,
                static_pulls=not args.power_idle_high_z)
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
            help="keep the card active while the target rail changes")
        parser.add_argument("--power-off-threshold", type=float, default=None, metavar="V",
            help="treat target rail as off below V (high-Z default: 2.7; normal: 1.0)")
        parser.add_argument("--power-on-threshold", type=float, default=None, metavar="V",
            help="treat target rail as on above V (high-Z default: 3.0; normal: 1.8)")
        parser.add_argument("--power-poll-interval", type=float, default=None, metavar="SECONDS",
            help="target-power polling interval (high-Z default: 0.01; normal: 0.25)")
        parser.add_argument("--power-debounce-samples", type=int, default=None, metavar="COUNT",
            help="samples required for a transition (high-Z default: 2; normal: 3)")
        parser.add_argument("--power-resume-settle", type=float, default=0.005, metavar="SECONDS",
            help="wait after restoring pulls before enabling the card (default: 0.005)")
        parser.add_argument("--power-rearm-min-interval", type=float, default=10.0,
            metavar="SECONDS",
            help="suppress repeated engine resets during boot-time rail chatter "
                 "(default: 10.0)")
        parser.add_argument("--power-rearm-reset-hold", type=float, default=0.05,
            metavar="SECONDS",
            help="hold all SD and host-transport state in reset before off-rail rearming "
                 "(default: 0.05)")
        parser.add_argument("--diagnostics-interval", type=float, default=0.0,
            metavar="SECONDS",
            help="periodically log FPGA and backend diagnostics (default: disabled)")
        parser.add_argument("--trace-commands", action="store_true",
            help="log command-counter/last-command transitions for target debugging")

    async def setup(self, args):
        if args.power_idle_high_z and args.no_power_monitor:
            raise SDCardError("--power-idle-high-z cannot be used with --no-power-monitor")
        if args.power_idle_high_z:
            vio = args.voltage.get(self._sd_port)
            if vio is None or vio.value is None or vio.value == 0:
                raise SDCardError(
                    "--power-idle-high-z requires a fixed nonzero Vio, such as -V B=3.3")

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
        elif args.power_idle_high_z:
            # The SD breakout routes target VDD only to SENSE; Glasgow Vio powers the level
            # translators and is isolated. Keep it enabled, widen any stale alert left by a
            # sense-following run, remove every sourcing pull, and hold CLK weak-low before
            # opening the image.
            await self.device.reset_alert(str(self._sd_port))
            await self.device.poll_alert()
            await self.sd_card_iface.set_signal_pulls(
                self.device, self._sd_port, enabled=False)
            sense_voltage = await self.device.measure_voltage(str(self._sd_port))
            self.logger.info(
                "port %s armed in target-off state: Vio on, card listening, "
                "CMD/DAT high-impedance, CLK weak-low, sense %.2f V",
                self._sd_port, sense_voltage)
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

        async def trace_commands():
            previous_command = None
            while True:
                # This register already exists independently of the packed diagnostic snapshot,
                # so command tracing remains usable with a prebuilt bitstream. Poll it alone to
                # keep the sampling interval below a legacy 1 Mbit/s single-block transfer; read
                # the counter only after a transition, where it is useful context rather than a
                # prerequisite for observing the command.
                last_command = await self.sd_card_iface.last_command
                if last_command != previous_command:
                    if args.capture_cmd17_wire and previous_command == 17:
                        wire_low = await self.sd_card_iface.clock_count
                        wire_high = await self.sd_card_iface.command_low_count
                        wire_response = ((wire_high << 32) | wire_low) & ((1 << 48) - 1)
                        self.logger.info(
                            "CMD17 pad-observed R1: %012x", wire_response)
                    command_count = await self.sd_card_iface.command_count
                    self.logger.info(
                        "SD command trace: count~%d last=CMD%d",
                        command_count, last_command)
                    previous_command = last_command
                # Leave headroom for the 10 ms target-power monitor and the block-service
                # worker; an unthrottled USB register loop can otherwise delay a SENSE edge.
                await asyncio.sleep(0.001)

        high_z = args.power_idle_high_z
        off_threshold = args.power_off_threshold
        on_threshold = args.power_on_threshold
        interval = args.power_poll_interval
        debounce_samples = args.power_debounce_samples
        if off_threshold is None:
            off_threshold = 2.7 if high_z else 1.0
        if on_threshold is None:
            on_threshold = 3.0 if high_z else 1.8
        if interval is None:
            interval = 0.01 if high_z else 0.25
        if debounce_samples is None:
            debounce_samples = 2 if high_z else 3

        try:
            if args.no_power_monitor and args.diagnostics_interval <= 0:
                await self.sd_card_iface.serve()
                return
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(self.sd_card_iface.serve())
                if args.trace_commands:
                    tasks.create_task(trace_commands())
                if not args.no_power_monitor:
                    tasks.create_task(self.sd_card_iface.monitor_power(
                        self.device, self._sd_port,
                        off_threshold=off_threshold,
                        on_threshold=on_threshold,
                        interval=interval,
                        debounce_samples=debounce_samples,
                        manage_signal_pulls=high_z,
                        resume_settle=args.power_resume_settle,
                        rearm_min_interval=args.power_rearm_min_interval,
                        rearm_reset_hold=args.power_rearm_reset_hold))
                if args.diagnostics_interval > 0:
                    tasks.create_task(log_diagnostics())
        finally:
            if high_z:
                await self.sd_card_iface.set_signal_pulls(
                    self.device, self._sd_port, enabled=False)

    @classmethod
    def tests(cls):
        from . import test
        return test.SDCardAppletTestCase
