from amaranth import *
from amaranth.lib import cdc, wiring
from amaranth.lib.wiring import In, Out

from ._resume_probe import RESUME_PROBE_BIT_CYCLES


__all__ = ["EARLY_MARKER_BIT_CYCLES", "EarlyMarkerCapture"]


EARLY_MARKER_BIT_CYCLES = RESUME_PROBE_BIT_CYCLES


class EarlyMarkerCapture(wiring.Component):
    """Compact input-only decoder for early markers on the SD CLK pad.

    The wire format is the qualified deep-resume marker frame: physically
    inverted 115200-8N1 carrying ``55 d3 4d 01 seq type ~type crc8``. Only the
    last nonzero, structurally valid type is retained. The SD card engine
    continues to observe the same pad independently; this block never drives
    any SD net.
    """

    clk_i: In(1)
    last_marker: Out(8)

    def __init__(self, *, bit_cycles=EARLY_MARKER_BIT_CYCLES):
        self._bit_cycles = bit_cycles
        super().__init__()

    def elaborate(self, platform):
        m = Module()

        clk_sync = Signal()
        m.submodules.clk_sync = cdc.FFSynchronizer(self.clk_i, clk_sync)

        level_q = Signal()
        m.d.sync += level_q.eq(clk_sync)

        rx_active = Signal()
        rx_timer = Signal(range(self._bit_cycles * 2))
        rx_bit = Signal(range(9))
        rx_shift = Signal(8)
        rx_byte = Signal(8)
        rx_byte_valid = Signal()
        m.d.sync += rx_byte_valid.eq(0)

        with m.If(~rx_active):
            # Logical UART start is low, hence physical high on this inverted
            # target-output wire. Each byte resynchronizes on its own edge.
            with m.If(clk_sync & ~level_q):
                m.d.sync += [
                    rx_active.eq(1),
                    rx_timer.eq(self._bit_cycles + self._bit_cycles // 2 - 1),
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
                    rx_timer.eq(self._bit_cycles - 1),
                ]
            with m.Else():
                m.d.sync += rx_active.eq(0)
                with m.If(~clk_sync):
                    m.d.sync += [
                        rx_byte.eq(rx_shift),
                        rx_byte_valid.eq(1),
                    ]

        parser_state = Signal(range(8))
        frame_type = Signal(8)

        with m.If(rx_byte_valid):
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
                        m.d.sync += parser_state.eq(4)
                    with m.Else():
                        m.d.sync += parser_state.eq(Mux(rx_byte == 0x55, 1, 0))
                with m.Case(4):
                    m.d.sync += parser_state.eq(5)
                with m.Case(5):
                    m.d.sync += [
                        frame_type.eq(rx_byte),
                        parser_state.eq(6),
                    ]
                with m.Case(6):
                    with m.If((rx_byte == (frame_type ^ 0xff)) & (frame_type != 0)):
                        m.d.sync += parser_state.eq(7)
                    with m.Else():
                        m.d.sync += parser_state.eq(Mux(rx_byte == 0x55, 1, 0))
                with m.Case(7):
                    # Consume the existing frame's CRC byte for wire compatibility. The compact
                    # in-card decoder relies on the 24-bit magic, version, and type complement;
                    # the dedicated probe applet retains the full CRC checker and raw edge trace.
                    m.d.sync += [
                        self.last_marker.eq(frame_type),
                        parser_state.eq(Mux(rx_byte == 0x55, 1, 0)),
                    ]

        return m
