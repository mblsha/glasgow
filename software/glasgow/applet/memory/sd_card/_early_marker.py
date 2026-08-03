from amaranth import *
from amaranth.lib import cdc, wiring
from amaranth.lib.wiring import In, Out


__all__ = [
    "EARLY_MARKER_BREAK_MIN",
    "EARLY_MARKER_SYNC_MIN",
    "EARLY_MARKER_SYNC_MAX",
    "EARLY_MARKER_SHORT_MIN",
    "EARLY_MARKER_SHORT_MAX",
    "EARLY_MARKER_LONG_MIN",
    "EARLY_MARKER_LONG_MAX",
    "EarlyMarkerCapture",
]


# Glasgow's sync domain is 48 MHz. The target-side transmitter uses the
# 3.6864 MHz PXA OS timer: 128 ticks for BREAK/SYNC, 32 ticks for a short
# interval, and 64 ticks for a long interval. These windows leave ample room
# for synchronizer latency and software GPIO transition jitter.
EARLY_MARKER_BREAK_MIN = 1400
EARLY_MARKER_SYNC_MIN = 1400
EARLY_MARKER_SYNC_MAX = 1950
EARLY_MARKER_SHORT_MIN = 320
EARLY_MARKER_SHORT_MAX = 560
EARLY_MARKER_LONG_MIN = 700
EARLY_MARKER_LONG_MAX = 1050


class EarlyMarkerCapture(wiring.Component):
    """Minimal input-only pulse decoder for early markers on SD CLK.

    A frame is a long low BREAK, a long high SYNC, then eight pulse-width
    bits, least significant first. Each bit has a short low gap followed by a
    short high pulse for zero or a long high pulse for one. Bits 0..3 are a
    nonzero marker and bits 4..7 are its one's complement. Only the last valid
    marker is retained.

    The SD card engine observes the same pad independently. This component
    has no output or output-enable path and never drives any SD net.
    """

    clk_i: In(1)
    last_marker: Out(4)

    def __init__(self, *,
            break_min=EARLY_MARKER_BREAK_MIN,
            sync_min=EARLY_MARKER_SYNC_MIN, sync_max=EARLY_MARKER_SYNC_MAX,
            short_min=EARLY_MARKER_SHORT_MIN, short_max=EARLY_MARKER_SHORT_MAX,
            long_min=EARLY_MARKER_LONG_MIN, long_max=EARLY_MARKER_LONG_MAX):
        assert 0 < break_min
        assert 0 < sync_min <= sync_max
        assert 0 < short_min <= short_max < long_min <= long_max
        self._break_min = break_min
        self._sync_min = sync_min
        self._sync_max = sync_max
        self._short_min = short_min
        self._short_max = short_max
        self._long_min = long_min
        self._long_max = long_max
        super().__init__()

    def elaborate(self, platform):
        m = Module()

        clk_sync = Signal()
        m.submodules.clk_sync = cdc.FFSynchronizer(self.clk_i, clk_sync)

        level_q = Signal()
        m.d.sync += level_q.eq(clk_sync)
        rising = clk_sync & ~level_q
        falling = ~clk_sync & level_q
        edge = rising | falling

        duration_width = max(
            self._break_min, self._sync_max,
            self._short_max, self._long_max,
        ).bit_length()
        duration = Signal(duration_width)
        with m.If(edge):
            m.d.sync += duration.eq(0)
        with m.Elif(~duration.all()):
            m.d.sync += duration.eq(duration + 1)

        SEARCH_BREAK = 0
        SYNC_HIGH = 1
        BIT_LOW = 2
        BIT_HIGH = 3
        state = Signal(2)
        bit_index = Signal(3)
        frame_bits = Signal(7)

        short_interval = (duration >= self._short_min) & (duration <= self._short_max)
        long_interval = (duration >= self._long_min) & (duration <= self._long_max)
        candidate = Cat(frame_bits, long_interval)

        with m.Switch(state):
            with m.Case(SEARCH_BREAK):
                with m.If(rising & (duration >= self._break_min)):
                    m.d.sync += state.eq(SYNC_HIGH)

            with m.Case(SYNC_HIGH):
                with m.If(falling):
                    with m.If((duration >= self._sync_min) & (duration <= self._sync_max)):
                        m.d.sync += [
                            state.eq(BIT_LOW),
                            bit_index.eq(0),
                            frame_bits.eq(0),
                        ]
                    with m.Else():
                        m.d.sync += state.eq(SEARCH_BREAK)

            with m.Case(BIT_LOW):
                with m.If(rising):
                    with m.If(short_interval):
                        m.d.sync += state.eq(BIT_HIGH)
                    with m.Else():
                        m.d.sync += state.eq(SEARCH_BREAK)

            with m.Case(BIT_HIGH):
                with m.If(falling):
                    with m.If(short_interval | long_interval):
                        with m.If(bit_index == 7):
                            marker = candidate[:4]
                            with m.If((marker != 0) & (candidate[4:] == (marker ^ 0xf))):
                                m.d.sync += self.last_marker.eq(marker)
                            m.d.sync += state.eq(SEARCH_BREAK)
                        with m.Else():
                            m.d.sync += [
                                frame_bits.bit_select(bit_index, 1).eq(long_interval),
                                bit_index.eq(bit_index + 1),
                                state.eq(BIT_LOW),
                            ]
                    with m.Else():
                        m.d.sync += state.eq(SEARCH_BREAK)

        return m
