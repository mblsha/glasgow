from amaranth import *
from amaranth.lib import data, wiring, stream, crc
from amaranth.lib.wiring import In, Out


__all__ = [
    "ChecksumAppender", "ChecksumVerifier", "FrameChecksumAppender", "FrameChecksumVerifier",
]


class ChecksumAppender(wiring.Component):
    """Appends a CRC checksum to packets.

    Packet data is cut through with a latency of 0. After the last payload of the packet,
    the CRC value is output from LSB to MSB. (This order enables verifying the packet checksum
    by comparing the running CRC with the residue.)
    """

    def __init__(self, algorithm: crc.Algorithm, *, data_width=8):
        if algorithm.crc_width % data_width != 0:
            raise ValueError("CRC width must be divisible by data width")

        self._parameters = algorithm(data_width)

        super().__init__({
            "i": In(stream.Signature(data.StructLayout({
                "data":  data_width,
                "first": 1,
                "last":  1,
            }))),
            "o": Out(stream.Signature(data.StructLayout({
                "data":  data_width,
                "first": 1,
                "last":  1,
            })))
        })

    def elaborate(self, platform):
        m = Module()

        m.submodules.engine = engine = self._parameters.create()
        m.d.comb += engine.data.eq(self.i.p.data)

        with m.FSM():
            with m.State("Data"):
                wiring.connect(m, wiring.flipped(self.o), wiring.flipped(self.i))
                with m.If(self.i.valid & self.i.ready):
                    m.d.comb += engine.valid.eq(1)
                    m.d.comb += engine.start.eq(self.i.p.first)
                    with m.If(self.i.p.last):
                        m.d.comb += self.o.p.last.eq(0)
                        m.next = "CRC"

            with m.State("CRC"):
                length = len(engine.crc) // len(self.o.p.data)
                offset = Signal(range(length))

                m.d.comb += self.o.p.data.eq(engine.crc.word_select(offset, len(self.o.p.data)))
                m.d.comb += self.o.p.last.eq(offset == length - 1)
                m.d.comb += self.o.valid.eq(1)
                with m.If(self.o.ready):
                    m.d.sync += offset.eq(offset + 1)
                    with m.If(self.o.p.last):
                        m.next = "Data"

        return m


class ChecksumVerifier(wiring.Component):
    """Verifies a CRC checksum at the end of a packet.

    Packet data is cut through with a latency of ``algorithm.crc_width // data_width + 1``, with
    the checksum removed. ``last`` is asserted for the last payload of the packet if and only if
    the checksum matches.
    """

    def __init__(self, algorithm: crc.Algorithm, *, data_width=8):
        if algorithm.crc_width % data_width != 0:
            raise ValueError("CRC width must be divisible by data width")

        self._parameters = algorithm(data_width)

        super().__init__({
            "i": In(stream.Signature(data.StructLayout({
                "data":  data_width,
                "first": 1,
                "last":  1,
                "end":   1,
            }))),
            "o": Out(stream.Signature(data.StructLayout({
                "data":  data_width,
                "first": 1,
                "last":  1,
            })))
        })

    def elaborate(self, platform):
        m = Module()

        m.submodules.engine = engine = self._parameters.create()

        length = len(engine.crc) // len(self.o.p.data)
        buffer = Signal(data.ArrayLayout(self.i.p.data.shape(), length + 1))
        offset = Signal(range(len(buffer) + 1))

        m.d.comb += [
            engine.data.eq(self.i.p.data),
            engine.start.eq(self.i.valid & ~self.i.p.end & self.i.p.first),
            engine.valid.eq(self.i.valid & ~self.i.p.end & self.i.ready),
            self.o.p.data.eq(buffer[-1]),
            self.o.p.first.eq(offset == length),
        ]
        with m.FSM():
            with m.State("Data"):
                m.d.comb += self.o.valid.eq(
                    self.i.valid & (offset >= length) & ~(self.i.p.first & ~self.i.p.end))
                m.d.comb += self.i.ready.eq(
                    self.o.ready | (offset <  length) |  (self.i.p.first & ~self.i.p.end))
                with m.If(self.i.valid & self.i.ready):
                    m.d.sync += buffer.eq(Cat(self.i.p.data, buffer))
                    with m.If(self.i.p.first):
                        m.d.sync += offset.eq(0)
                    with m.Elif(offset <= length): # saturate at `length + 1`
                        m.d.sync += offset.eq(offset + 1)
                    with m.If((offset >= length - 1) & self.i.p.last):
                        m.next = "Last"
                    with m.If(self.i.p.end):
                        m.d.comb += self.o.p.last.eq(engine.match_detected & (offset >= length))
                        m.d.sync += offset.eq(0)

            with m.State("Last"):
                m.d.comb += self.o.valid.eq(1)
                m.d.comb += self.o.p.last.eq(engine.match_detected)
                with m.If(self.o.ready):
                    m.d.sync += offset.eq(0)
                    m.next = "Data"

        return m


class FrameChecksumAppender(wiring.Component):
    """Append a CRC checksum to an explicitly delimited token stream.

    Data tokens are passed through unchanged. An input ``end`` token is replaced with the
    checksum, least-significant word first, followed by an output ``end`` token. Unlike
    :class:`ChecksumAppender`, this component does not require the producer to predict which data
    token is last; this is useful for packet producers whose final byte is selected by an FSM.
    """

    def __init__(self, algorithm: crc.Algorithm, *, data_width=8):
        if algorithm.crc_width % data_width != 0:
            raise ValueError("CRC width must be divisible by data width")

        self._parameters = algorithm(data_width)
        self._checksum_words = algorithm.crc_width // data_width
        token_layout = data.StructLayout({"data": data_width, "end": 1})
        super().__init__({
            "i": In(stream.Signature(token_layout)),
            "o": Out(stream.Signature(token_layout)),
        })

    def elaborate(self, platform):
        m = Module()

        m.submodules.engine = engine = self._parameters.create()
        first = Signal(init=1)
        offset = Signal(range(self._checksum_words))

        m.d.comb += engine.data.eq(self.i.p.data)
        with m.FSM():
            with m.State("Data"):
                m.d.comb += [
                    self.i.ready.eq(Mux(self.i.p.end, 1, self.o.ready)),
                    self.o.valid.eq(self.i.valid & ~self.i.p.end),
                    self.o.p.data.eq(self.i.p.data),
                ]
                with m.If(self.i.valid & self.i.ready):
                    with m.If(self.i.p.end):
                        m.d.sync += offset.eq(0)
                        m.next = "Checksum"
                    with m.Else():
                        m.d.comb += [
                            engine.start.eq(first),
                            engine.valid.eq(1),
                        ]
                        m.d.sync += first.eq(0)

            with m.State("Checksum"):
                m.d.comb += [
                    self.o.valid.eq(1),
                    self.o.p.data.eq(engine.crc.word_select(offset, len(self.o.p.data))),
                ]
                with m.If(self.o.ready):
                    with m.If(offset == self._checksum_words - 1):
                        m.next = "End"
                    with m.Else():
                        m.d.sync += offset.eq(offset + 1)

            with m.State("End"):
                m.d.comb += [
                    self.o.valid.eq(1),
                    self.o.p.end.eq(1),
                ]
                with m.If(self.o.ready):
                    m.d.sync += first.eq(1)
                    m.next = "Data"

        return m


class FrameChecksumVerifier(wiring.Component):
    """Verify and remove a CRC checksum from an explicitly delimited token stream.

    The output preserves each frame boundary and adds an ``error`` bit to its ``end`` token.
    Frames shorter than the checksum and frames with an invalid checksum are discarded at the
    boundary with ``error`` asserted. Payload bytes are otherwise streamed with only enough
    lookbehind to retain the checksum.
    """

    def __init__(self, algorithm: crc.Algorithm, *, data_width=8):
        if algorithm.crc_width % data_width != 0:
            raise ValueError("CRC width must be divisible by data width")

        self._parameters = algorithm(data_width)
        self._checksum_words = algorithm.crc_width // data_width
        input_layout = data.StructLayout({"data": data_width, "end": 1})
        output_layout = data.StructLayout({"data": data_width, "end": 1, "error": 1})
        super().__init__({
            "i": In(stream.Signature(input_layout)),
            "o": Out(stream.Signature(output_layout)),
            "abort": In(1),
        })

    def elaborate(self, platform):
        m = Module()

        m.submodules.engine = engine = self._parameters.create()
        checksum_words = self._checksum_words
        window = Signal(data.ArrayLayout(self.i.p.data.shape(), checksum_words))
        count = Signal(range(checksum_words + 1))
        first = Signal(init=1)
        frame_error = Signal()

        m.d.comb += engine.data.eq(window[0])
        with m.FSM():
            with m.State("Data"):
                with m.If(self.abort):
                    m.d.sync += [
                        frame_error.eq(1),
                        count.eq(0),
                    ]
                    m.next = "End"
                with m.Elif(self.i.p.end):
                    m.d.comb += self.i.ready.eq(1)
                    with m.If(self.i.valid):
                        m.d.sync += [
                            frame_error.eq(
                                (count != checksum_words) |
                                (window.as_value() != engine.crc)),
                            count.eq(0),
                        ]
                        m.next = "End"
                with m.Elif(count < checksum_words):
                    m.d.comb += self.i.ready.eq(1)
                    with m.If(self.i.valid):
                        m.d.sync += [
                            window[count].eq(self.i.p.data),
                            count.eq(count + 1),
                        ]
                with m.Else():
                    m.d.comb += [
                        self.i.ready.eq(self.o.ready),
                        self.o.valid.eq(self.i.valid),
                        self.o.p.data.eq(window[0]),
                    ]
                    with m.If(self.i.valid & self.i.ready):
                        m.d.comb += [
                            engine.start.eq(first),
                            engine.valid.eq(1),
                        ]
                        m.d.sync += [
                            window.eq(Cat(window[1:], self.i.p.data)),
                            first.eq(0),
                        ]

            with m.State("End"):
                m.d.comb += [
                    self.o.valid.eq(1),
                    self.o.p.end.eq(1),
                    self.o.p.error.eq(frame_error),
                ]
                with m.If(self.o.ready):
                    m.d.sync += first.eq(1)
                    m.next = "Data"

        return m
