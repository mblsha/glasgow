import asyncio
from dataclasses import asdict, dataclass
import json
import logging
from pathlib import Path

from amaranth import *
from amaranth.lib import io, wiring
from amaranth.lib.wiring import In, Out

from glasgow.abstract import AbstractAssembly, GlasgowPin
from glasgow.applet import GlasgowAppletV2

from ..sd_card._errors import SDCardError
from ..sd_card._resume_probe import (
    RESUME_PROBE_BIT_CYCLES, RESUME_PROBE_RAW_DEPTH, ResumeProbeCapture,
)


__all__ = [
    "SDResumeProbeApplet", "SDResumeProbeComponent", "SDResumeProbeInterface",
    "ResumeProbeStatus", "crc8_atm", "decode_raw_marker_frames",
]


@dataclass(frozen=True)
class ResumeProbeStatus:
    armed: bool
    gate_ack: bool
    idle_low: bool
    input_only_mask: int
    generation: int
    arm_timestamp: int
    raw_count: int
    edge_count: int
    dropped_edges: int
    timestamp_overflows: int
    decoded_bytes: int
    valid_frames: int
    last_sequence: int
    last_type: int
    last_frame_timestamp: int
    framing_errors: int
    protocol_errors: int


def crc8_atm(payload):
    crc = 0
    for octet in payload:
        crc ^= octet
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xff if crc & 0x80 else (crc << 1) & 0xff
    return crc


def decode_raw_marker_frames(status, records):
    """Independently decode retained B2 edges into compact marker frames."""
    if status.timestamp_overflows or status.dropped_edges:
        raise SDCardError(
            "raw probe trace is lossy and cannot be independently decoded")
    if len(records) != status.raw_count:
        raise SDCardError(
            f"raw record count mismatch {len(records)} != {status.raw_count}")

    transitions = [(status.arm_timestamp, 0)]
    previous_tick = status.arm_timestamp
    previous_level = 0
    for record in records:
        tick = int(record["tick"])
        level = int(record["level"])
        if tick <= previous_tick:
            raise SDCardError("raw probe ticks are not strictly monotonic")
        if level not in (0, 1) or level == previous_level:
            raise SDCardError("raw probe record has an impossible level transition")
        transitions.append((tick, level))
        previous_tick = tick
        previous_level = level

    def level_at(tick):
        level = 0
        for edge_tick, edge_level in transitions:
            if edge_tick > tick:
                break
            level = edge_level
        return level

    bytes_with_ticks = []
    consumed_until = status.arm_timestamp
    bit_cycles = 48_000_000 / 115_200
    for index in range(1, len(transitions)):
        start_tick, level = transitions[index]
        if level != 1 or start_tick < consumed_until:
            continue
        data = 0
        for bit in range(8):
            sample_tick = round(start_tick + (1.5 + bit) * bit_cycles)
            data |= (not level_at(sample_tick)) << bit
        stop_tick = round(start_tick + 9.5 * bit_cycles)
        if level_at(stop_tick) != 0:
            continue
        bytes_with_ticks.append((int(data), start_tick))
        consumed_until = round(start_tick + 9.75 * bit_cycles)

    frames = []
    octets = bytes(value for value, _ in bytes_with_ticks)
    for offset in range(max(0, len(octets) - 7)):
        candidate = octets[offset:offset + 8]
        if candidate[:3] != b"\x55\xd3\x4d":
            continue
        version, sequence, frame_type, complement, crc = candidate[3:]
        body = candidate[3:7]
        if version != 1:
            continue
        if complement != frame_type ^ 0xff:
            continue
        if crc != crc8_atm(body):
            continue
        frames.append({
            "raw_byte_offset": offset,
            "start_tick": bytes_with_ticks[offset][1],
            "sequence": sequence,
            "type": frame_type,
            "crc": crc,
        })

    cell_measurements = []
    for (tick_a, _), (tick_b, _) in zip(transitions, transitions[1:]):
        delta = tick_b - tick_a
        cells = max(1, round(delta / bit_cycles))
        cell_measurements.append(delta / cells)
    timing = {
        "nominal_ticks": bit_cycles,
        "minimum_ticks": min(cell_measurements) if cell_measurements else None,
        "mean_ticks": (
            sum(cell_measurements) / len(cell_measurements)
            if cell_measurements else None),
        "maximum_ticks": max(cell_measurements) if cell_measurements else None,
    }
    return {
        "bytes": [value for value, _ in bytes_with_ticks],
        "frames": frames,
        "timing": timing,
    }


class SDResumeProbeComponent(wiring.Component):
    armed: In(1)
    generation: In(16)
    raw_index: In(range(RESUME_PROBE_RAW_DEPTH))

    gate_ack: Out(1)
    idle_low: Out(1)
    input_only_mask: Out(6)
    arm_generation: Out(16)
    arm_timestamp: Out(48)
    raw_record: Out(32)
    raw_count: Out(range(RESUME_PROBE_RAW_DEPTH + 1))
    edge_count: Out(32)
    dropped_edges: Out(32)
    timestamp_overflows: Out(16)
    decoded_bytes: Out(32)
    valid_frames: Out(32)
    last_sequence: Out(8)
    last_type: Out(8)
    last_frame_timestamp: Out(48)
    framing_errors: Out(16)
    protocol_errors: Out(16)

    def __init__(self, ports):
        self._ports = ports
        super().__init__()

    def elaborate(self, platform):
        m = Module()

        # Every SD net is instantiated as input-only. There is no output value
        # or output-enable signal anywhere in this applet's elaborated design.
        m.submodules.clk_buffer = clk_buffer = io.Buffer("i", self._ports.clk)
        m.submodules.cmd_buffer = io.Buffer("i", self._ports.cmd)
        for index, dat_port in enumerate(self._ports.dat):
            m.submodules[f"dat_buffer_{index}"] = io.Buffer("i", dat_port)

        m.submodules.capture = capture = ResumeProbeCapture()
        m.d.comb += [
            capture.armed.eq(self.armed),
            capture.generation.eq(self.generation),
            capture.clk_i.eq(clk_buffer.i),
            capture.raw_index.eq(self.raw_index),
            self.gate_ack.eq(capture.gate_ack),
            self.idle_low.eq(capture.idle_low),
            self.input_only_mask.eq(0x3f),
            self.arm_generation.eq(capture.arm_generation),
            self.arm_timestamp.eq(capture.arm_timestamp),
            self.raw_record.eq(capture.raw_record),
            self.raw_count.eq(capture.raw_count),
            self.edge_count.eq(capture.edge_count),
            self.dropped_edges.eq(capture.dropped_edges),
            self.timestamp_overflows.eq(capture.timestamp_overflows),
            self.decoded_bytes.eq(capture.decoded_bytes),
            self.valid_frames.eq(capture.valid_frames),
            self.last_sequence.eq(capture.last_sequence),
            self.last_type.eq(capture.last_type),
            self.last_frame_timestamp.eq(capture.last_frame_timestamp),
            self.framing_errors.eq(capture.framing_errors),
            self.protocol_errors.eq(capture.protocol_errors),
        ]
        return m


class SDResumeProbeInterface:
    def __init__(self, logger: logging.Logger, assembly: AbstractAssembly, *,
            clk: GlasgowPin, cmd: GlasgowPin, dat: tuple[GlasgowPin, ...]):
        self._logger = logger
        ports = assembly.add_port_group(clk=clk, cmd=cmd, dat=dat)
        self._clk_pin_number = clk.number
        component = assembly.add_submodule(SDResumeProbeComponent(ports))

        self._armed = assembly.add_rw_register(component.armed)
        self._generation = assembly.add_rw_register(component.generation)
        self._raw_index = assembly.add_rw_register(component.raw_index)
        self._gate_ack = assembly.add_ro_register(component.gate_ack)
        self._idle_low = assembly.add_ro_register(component.idle_low)
        self._input_only_mask = assembly.add_ro_register(component.input_only_mask)
        self._arm_generation = assembly.add_ro_register(component.arm_generation)
        self._arm_timestamp = assembly.add_ro_register(component.arm_timestamp)
        self._raw_record = assembly.add_ro_register(component.raw_record)
        self._raw_count = assembly.add_ro_register(component.raw_count)
        self._edge_count = assembly.add_ro_register(component.edge_count)
        self._dropped_edges = assembly.add_ro_register(component.dropped_edges)
        self._timestamp_overflows = assembly.add_ro_register(
            component.timestamp_overflows)
        self._decoded_bytes = assembly.add_ro_register(component.decoded_bytes)
        self._valid_frames = assembly.add_ro_register(component.valid_frames)
        self._last_sequence = assembly.add_ro_register(component.last_sequence)
        self._last_type = assembly.add_ro_register(component.last_type)
        self._last_frame_timestamp = assembly.add_ro_register(
            component.last_frame_timestamp)
        self._framing_errors = assembly.add_ro_register(component.framing_errors)
        self._protocol_errors = assembly.add_ro_register(component.protocol_errors)

    async def status(self):
        return ResumeProbeStatus(
            armed=bool(await self._armed),
            gate_ack=bool(await self._gate_ack),
            idle_low=bool(await self._idle_low),
            input_only_mask=await self._input_only_mask,
            generation=await self._arm_generation,
            arm_timestamp=await self._arm_timestamp,
            raw_count=await self._raw_count,
            edge_count=await self._edge_count,
            dropped_edges=await self._dropped_edges,
            timestamp_overflows=await self._timestamp_overflows,
            decoded_bytes=await self._decoded_bytes,
            valid_frames=await self._valid_frames,
            last_sequence=await self._last_sequence,
            last_type=await self._last_type,
            last_frame_timestamp=await self._last_frame_timestamp,
            framing_errors=await self._framing_errors,
            protocol_errors=await self._protocol_errors,
        )

    async def arm(self, device, port, *, generation, ram_root_attested,
            mmc_unbound_attested, sense_threshold=0.5, sense_samples=3):
        if not ram_root_attested or not mmc_unbound_attested:
            raise SDCardError(
                "probe arm requires explicit RAM-root and MMC-unbound attestations")
        if not 0 < generation <= 0xffff:
            raise SDCardError("probe generation must be in 1..65535")
        if sense_samples < 3:
            raise SDCardError("at least three SENSE samples are required")
        if await self._armed:
            raise SDCardError("probe is already armed")

        # CLK gets only a weak low pull; every sourcing CMD/DAT pull is absent.
        await device.set_pulls(
            str(port), low={self._clk_pin_number}, high=set())
        voltages = []
        for _ in range(sense_samples):
            voltages.append(await device.measure_voltage(str(port)))
            await asyncio.sleep(0.01)
        if any(voltage >= sense_threshold for voltage in voltages):
            raise SDCardError(
                f"SENSE qualification failed: {voltages!r} must all be below "
                f"{sense_threshold:.3f} V")

        await self._generation.set(generation)
        await self._armed.set(1)
        await asyncio.sleep(0.002)
        status = await self.status()
        if not status.gate_ack or status.input_only_mask != 0x3f:
            raise SDCardError(f"input-only arm acknowledgement failed: {status!r}")
        if status.generation != generation or not status.idle_low:
            raise SDCardError(f"probe generation/idle qualification failed: {status!r}")
        if (status.raw_count or status.edge_count or status.dropped_edges or
                status.timestamp_overflows or status.valid_frames or
                status.framing_errors or status.protocol_errors):
            raise SDCardError(f"probe did not arm empty: {status!r}")
        return status, tuple(voltages)

    async def drain(self):
        status = await self.status()
        if status.raw_count > RESUME_PROBE_RAW_DEPTH:
            raise SDCardError(f"impossible raw count {status.raw_count}")
        records = []
        tick = status.arm_timestamp
        for index in range(status.raw_count):
            await self._raw_index.set(index)
            await asyncio.sleep(0)
            record = await self._raw_record
            delta = record & 0x7fff_ffff
            level = (record >> 31) & 1
            if delta == 0:
                raise SDCardError(f"raw edge {index} has zero delta")
            tick += delta
            records.append({
                "index": index, "delta": delta,
                "tick": tick, "level": level,
            })
        decoded = decode_raw_marker_frames(status, records)
        return status, records, decoded

    async def disarm(self):
        status, records, decoded = await self.drain()
        await self._armed.set(0)
        await asyncio.sleep(0.002)
        if await self._gate_ack:
            raise SDCardError("probe acknowledgement remained high after disarm")
        return status, records, decoded


class SDResumeProbeApplet(GlasgowAppletV2):
    logger = logging.getLogger(__name__)
    help = "capture SL-C860 deep-resume markers on the SD CLK net"
    description = """
    Capture electrically inverted 115200-8N1 deep-resume marker frames from
    PXA255 GPIO6 through the SL-C860 SD CLK net. Every SD pin is input-only.
    This revC3-only diagnostic applet must be loaded only after the disposable
    RAM-root target has unbound MMC and three B-SENSE samples are below 0.5 V.
    """
    required_revision = "C3"
    preview = True

    @classmethod
    def add_build_arguments(cls, parser, access):
        access.add_voltage_argument(parser)
        access.add_pins_argument(parser, "clk", default="B2", required=True)
        access.add_pins_argument(parser, "cmd", default="B5", required=True)
        access.add_pins_argument(
            parser, "dat", width=4, default="B1,B0,B7,B6", required=True)

    def build(self, args):
        with self.assembly.add_applet(self):
            self.assembly.use_voltage(args.voltage)
            self.probe_iface = SDResumeProbeInterface(
                self.logger, self.assembly,
                clk=args.clk, cmd=args.cmd, dat=args.dat)
            self._port = args.clk.port
            self._clk_pin_number = args.clk.number

    @classmethod
    def add_setup_arguments(cls, parser):
        parser.add_argument("--control", type=Path, required=True, metavar="SOCKET",
            help="serve newline-delimited JSON control on local Unix SOCKET")

    async def setup(self, args):
        if self.device.revision != "C3":
            raise SDCardError(
                f"SD resume probe is qualified only on revC3, not "
                f"{self.device.revision}")
        vio = args.voltage.get(self._port)
        if vio is None or vio.value != 3.3:
            raise SDCardError("SD resume probe requires exact fixed -V B=3.3")
        if args.control.exists():
            raise SDCardError(f"control socket already exists: {args.control}")
        await self.device.reset_alert(str(self._port))
        await self.device.poll_alert()
        await self.device.set_pulls(
            str(self._port), low={self._clk_pin_number}, high=set())

    async def run(self, args):
        async def client(reader, writer):
            try:
                while request_line := await reader.readline():
                    try:
                        request = json.loads(request_line)
                        command = request.get("command")
                        if command == "status":
                            status = await self.probe_iface.status()
                            response = {
                                "ok": True,
                                "status": asdict(status),
                                "sense_voltage": await self.device.measure_voltage(
                                    str(self._port)),
                            }
                        elif command == "arm":
                            status, voltages = await self.probe_iface.arm(
                                self.device, self._port,
                                generation=int(request["generation"]),
                                ram_root_attested=bool(
                                    request.get("ram_root_attested", False)),
                                mmc_unbound_attested=bool(
                                    request.get("mmc_unbound_attested", False)),
                                sense_threshold=float(
                                    request.get("sense_threshold", 0.5)),
                                sense_samples=int(request.get("sense_samples", 3)))
                            response = {
                                "ok": True, "status": asdict(status),
                                "sense_voltages": voltages,
                            }
                        elif command in ("drain", "disarm"):
                            operation = (
                                self.probe_iface.drain if command == "drain"
                                else self.probe_iface.disarm)
                            status, records, decoded = await operation()
                            response = {
                                "ok": True, "status": asdict(status),
                                "records": records, "decoded": decoded,
                            }
                            if command == "disarm":
                                # ``status`` is intentionally the immutable
                                # pre-disarm evidence snapshot. Expose the
                                # post-transition state as well so a saved
                                # response proves that the fail-closed gate
                                # acknowledgement actually fell.
                                response["post_status"] = asdict(
                                    await self.probe_iface.status())
                        else:
                            raise SDCardError(f"unknown command {command!r}")
                    except BaseException as error:
                        response = {
                            "ok": False,
                            "error": f"{error.__class__.__name__}: {error}",
                        }
                    writer.write(
                        json.dumps(response, sort_keys=True).encode() + b"\n")
                    await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        args.control.parent.mkdir(parents=True, exist_ok=True)
        server = await asyncio.start_unix_server(client, path=args.control)
        self.logger.info("SD resume-probe control listening at %s", args.control)
        try:
            async with server:
                await server.serve_forever()
        finally:
            try:
                args.control.unlink()
            except FileNotFoundError:
                pass

    @classmethod
    def tests(cls):
        from . import test
        return test.SDResumeProbeAppletTestCase
