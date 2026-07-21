from amaranth import *
from amaranth.lib import crc, data, stream, wiring
from amaranth.lib.wiring import In, Out

from .cobs import Decoder as COBSDecoder, Encoder as COBSEncoder
from .crc import FrameChecksumAppender, FrameChecksumVerifier


__all__ = ["COBSCRCFrameDecoder", "COBSCRCFrameEncoder"]


class COBSCRCFrameEncoder(wiring.Component):
    """Encode delimited data tokens as CRC-protected, NUL-delimited COBS frames."""

    def __init__(self, algorithm: crc.Algorithm, *, fifo_depth=256):
        self._algorithm = algorithm
        self._fifo_depth = fifo_depth
        token_layout = data.StructLayout({"data": 8, "end": 1})
        super().__init__({
            "i": In(stream.Signature(token_layout)),
            "o": Out(stream.Signature(8)),
        })

    def elaborate(self, platform):
        m = Module()

        m.submodules.checksum = checksum = FrameChecksumAppender(self._algorithm)
        m.submodules.cobs = cobs = COBSEncoder(fifo_depth=self._fifo_depth)
        wiring.connect(m, checksum.i, wiring.flipped(self.i))
        wiring.connect(m, cobs.i, checksum.o)
        wiring.connect(m, wiring.flipped(self.o), cobs.o)

        return m


class COBSCRCFrameDecoder(wiring.Component):
    """Decode COBS frames, verify their CRC, and recover at malformed frame boundaries.

    Every input delimiter produces exactly one handshaked output ``end`` token. Its ``error`` bit
    covers both malformed COBS and checksum failures, so downstream parsers need only discard the
    current frame and resume after accepting that boundary.
    """

    def __init__(self, algorithm: crc.Algorithm):
        self._algorithm = algorithm
        output_layout = data.StructLayout({"data": 8, "end": 1, "error": 1})
        super().__init__({
            "i": In(stream.Signature(8)),
            "o": Out(stream.Signature(output_layout)),
        })

    def elaborate(self, platform):
        m = Module()

        m.submodules.cobs = cobs = COBSDecoder(recover=True, error_tokens=True)
        m.submodules.checksum = checksum = FrameChecksumVerifier(self._algorithm)
        wiring.connect(m, cobs.i, wiring.flipped(self.i))
        wiring.connect(m, checksum.i, cobs.o)
        wiring.connect(m, wiring.flipped(self.o), checksum.o)

        return m
