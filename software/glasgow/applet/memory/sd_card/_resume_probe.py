from amaranth import *
from amaranth.lib import cdc, memory, wiring
from amaranth.lib.wiring import In, Out


__all__ = [
    "RESUME_PROBE_BIT_CYCLES", "RESUME_PROBE_RAW_DEPTH",
    "ResumeProbeCapture",
]


RESUME_PROBE_BIT_CYCLES = 417
RESUME_PROBE_RAW_DEPTH = 128


def _crc8_atm_byte(crc, octet):
    """Return CRC-8/ATM after one byte, MSB first."""
    value = crc
    for bit in range(7, -1, -1):
        feedback = value[7] ^ octet[bit]
        value = Mux(feedback, Cat(C(0, 1), value[:7]) ^ 0x07,
                    Cat(C(0, 1), value[:7]))
    return value


class ResumeProbeCapture(wiring.Component):
    """Fail-closed, sync-domain capture for inverted UART on the SD CLK pad.

    The target drives physical-low idle and physical-high start bits. The
    capture retains compact-frame decode state in registers and stores a
    bounded, immutable raw edge trace as 31-bit deltas plus the new level.
    A 48-bit arm timestamp anchors every raw trace.
    """

    armed: In(1)
    generation: In(16)
    clk_i: In(1)
    raw_index: In(range(RESUME_PROBE_RAW_DEPTH))

    gate_ack: Out(1)
    idle_low: Out(1)
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

    def elaborate(self, platform):
        m = Module()

        clk_sync = Signal()
        m.submodules.clk_sync = cdc.FFSynchronizer(self.clk_i, clk_sync)

        timestamp = Signal(48)
        m.d.sync += timestamp.eq(timestamp + 1)

        gate_cycles = Signal(4)
        with m.If(~self.armed):
            m.d.sync += gate_cycles.eq(0)
        with m.Elif(gate_cycles != 0xf):
            m.d.sync += gate_cycles.eq(gate_cycles + 1)
        m.d.comb += [
            self.gate_ack.eq(self.armed & gate_cycles[3]),
            self.idle_low.eq(~clk_sync),
        ]

        raw_memory = memory.Memory(shape=32, depth=RESUME_PROBE_RAW_DEPTH, init=[])
        raw_write = raw_memory.write_port()
        raw_read = raw_memory.read_port(domain="sync")
        m.submodules.raw_memory = raw_memory
        m.d.comb += [
            raw_read.addr.eq(self.raw_index),
            self.raw_record.eq(raw_read.data),
            raw_write.en.eq(0),
            raw_write.addr.eq(self.raw_count[:7]),
            raw_write.data.eq(0),
        ]

        armed_q = Signal()
        level_q = Signal()
        last_edge_timestamp = Signal(48)
        edge_delta = timestamp - last_edge_timestamp
        m.d.sync += armed_q.eq(self.armed)

        with m.If(self.armed & ~armed_q):
            m.d.sync += [
                self.arm_generation.eq(self.generation),
                self.arm_timestamp.eq(timestamp),
                self.raw_count.eq(0),
                self.edge_count.eq(0),
                self.dropped_edges.eq(0),
                self.timestamp_overflows.eq(0),
                self.decoded_bytes.eq(0),
                self.valid_frames.eq(0),
                self.last_sequence.eq(0),
                self.last_type.eq(0),
                self.last_frame_timestamp.eq(0),
                self.framing_errors.eq(0),
                self.protocol_errors.eq(0),
                level_q.eq(clk_sync),
                last_edge_timestamp.eq(timestamp),
            ]
        with m.Elif(self.armed & (clk_sync != level_q)):
            m.d.sync += [
                level_q.eq(clk_sync),
                last_edge_timestamp.eq(timestamp),
                self.edge_count.eq(self.edge_count + 1),
            ]
            with m.If(edge_delta[31:]):
                m.d.sync += self.timestamp_overflows.eq(
                    self.timestamp_overflows + 1)
            with m.Elif(self.raw_count < RESUME_PROBE_RAW_DEPTH):
                m.d.comb += [
                    raw_write.en.eq(1),
                    raw_write.addr.eq(self.raw_count),
                    raw_write.data.eq(Cat(edge_delta[:31], clk_sync)),
                ]
                m.d.sync += self.raw_count.eq(self.raw_count + 1)
            with m.Else():
                m.d.sync += self.dropped_edges.eq(self.dropped_edges + 1)

        # Decode physical-inverted 115200-8N1. Each byte resynchronizes at its
        # start edge, so rounding 48 MHz / 115200 to 417 clocks cannot
        # accumulate across a compact frame.
        rx_active = Signal()
        rx_timer = Signal(range(RESUME_PROBE_BIT_CYCLES * 2))
        rx_bit = Signal(range(9))
        rx_shift = Signal(8)
        rx_byte = Signal(8)
        rx_byte_valid = Signal()
        m.d.sync += rx_byte_valid.eq(0)

        with m.If(self.armed & ~armed_q):
            m.d.sync += [
                rx_active.eq(0),
                rx_timer.eq(0),
                rx_bit.eq(0),
                rx_shift.eq(0),
            ]
        with m.Elif(~self.armed):
            m.d.sync += rx_active.eq(0)
        with m.Elif(~rx_active):
            with m.If(clk_sync & ~level_q):
                m.d.sync += [
                    rx_active.eq(1),
                    rx_timer.eq(RESUME_PROBE_BIT_CYCLES +
                                RESUME_PROBE_BIT_CYCLES // 2 - 1),
                    rx_bit.eq(0),
                    rx_shift.eq(0),
                ]
        with m.Elif(rx_timer != 0):
            m.d.sync += rx_timer.eq(rx_timer - 1)
        with m.Else():
            with m.If(rx_bit < 8):
                m.d.sync += [
                    rx_shift.bit_select(rx_bit, 1).eq(~clk_sync),
                    rx_bit.eq(rx_bit + 1),
                    rx_timer.eq(RESUME_PROBE_BIT_CYCLES - 1),
                ]
            with m.Else():
                m.d.sync += rx_active.eq(0)
                with m.If(~clk_sync):
                    m.d.sync += [
                        rx_byte.eq(rx_shift),
                        rx_byte_valid.eq(1),
                        self.decoded_bytes.eq(self.decoded_bytes + 1),
                    ]
                with m.Else():
                    m.d.sync += self.framing_errors.eq(
                        self.framing_errors + 1)

        # Compact frame: 55 d3 4d 01 seq type ~type crc8(version..~type).
        parser_state = Signal(range(8))
        frame_sequence = Signal(8)
        frame_type = Signal(8)
        frame_complement = Signal(8)
        frame_crc = Signal(8)

        with m.If(self.armed & ~armed_q):
            m.d.sync += [
                parser_state.eq(0),
                frame_sequence.eq(0),
                frame_type.eq(0),
                frame_complement.eq(0),
                frame_crc.eq(0),
            ]
        with m.Elif(rx_byte_valid):
            with m.Switch(parser_state):
                with m.Case(0):
                    with m.If(rx_byte == 0x55):
                        m.d.sync += parser_state.eq(1)
                with m.Case(1):
                    with m.If(rx_byte == 0xd3):
                        m.d.sync += parser_state.eq(2)
                    with m.Elif(rx_byte != 0x55):
                        m.d.sync += parser_state.eq(0)
                with m.Case(2):
                    with m.If(rx_byte == 0x4d):
                        m.d.sync += parser_state.eq(3)
                    with m.Elif(rx_byte == 0x55):
                        m.d.sync += parser_state.eq(1)
                    with m.Else():
                        m.d.sync += parser_state.eq(0)
                with m.Case(3):
                    with m.If(rx_byte == 0x01):
                        m.d.sync += [
                            frame_crc.eq(_crc8_atm_byte(C(0, 8), rx_byte)),
                            parser_state.eq(4),
                        ]
                    with m.Else():
                        m.d.sync += [
                            self.protocol_errors.eq(self.protocol_errors + 1),
                            parser_state.eq(Mux(rx_byte == 0x55, 1, 0)),
                        ]
                with m.Case(4):
                    m.d.sync += [
                        frame_sequence.eq(rx_byte),
                        frame_crc.eq(_crc8_atm_byte(frame_crc, rx_byte)),
                        parser_state.eq(5),
                    ]
                with m.Case(5):
                    m.d.sync += [
                        frame_type.eq(rx_byte),
                        frame_crc.eq(_crc8_atm_byte(frame_crc, rx_byte)),
                        parser_state.eq(6),
                    ]
                with m.Case(6):
                    m.d.sync += [
                        frame_complement.eq(rx_byte),
                        frame_crc.eq(_crc8_atm_byte(frame_crc, rx_byte)),
                        parser_state.eq(7),
                    ]
                with m.Case(7):
                    with m.If((frame_complement == (frame_type ^ 0xff)) &
                              (rx_byte == frame_crc)):
                        m.d.sync += [
                            self.valid_frames.eq(self.valid_frames + 1),
                            self.last_sequence.eq(frame_sequence),
                            self.last_type.eq(frame_type),
                            self.last_frame_timestamp.eq(timestamp),
                        ]
                    with m.Else():
                        m.d.sync += self.protocol_errors.eq(
                            self.protocol_errors + 1)
                    m.d.sync += parser_state.eq(0)

        return m
