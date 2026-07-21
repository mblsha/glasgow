from amaranth import *
from amaranth.lib import data, stream, wiring
from amaranth.lib.wiring import In, Out

from ._protocol import REPLY_SCHEMA


__all__ = [
    "HostReplyWordPacker", "HOST_REPLY_WORD_LAYOUT", "HOST_REPLY_WORD_STREAM_SIGNATURE",
    "RawBlockWordPacker", "RAW_BLOCK_WORD_LAYOUT", "RAW_BLOCK_WORD_STREAM_SIGNATURE",
]


HOST_REPLY_WORD_LAYOUT = data.StructLayout({
    "data": 64,
    "count": 4,
    "end": 1,
    "error": 1,
})
HOST_REPLY_WORD_STREAM_SIGNATURE = stream.Signature(HOST_REPLY_WORD_LAYOUT)

RAW_BLOCK_WORD_LAYOUT = data.StructLayout({"data": 64})
RAW_BLOCK_WORD_STREAM_SIGNATURE = stream.Signature(RAW_BLOCK_WORD_LAYOUT)


class RawBlockWordPacker(wiring.Component):
    """Pack an unframed, block-aligned host byte stream into eight-byte SD-domain words."""

    i_stream: In(stream.Signature(8))
    o_stream: Out(RAW_BLOCK_WORD_STREAM_SIGNATURE)

    def elaborate(self, platform):
        m = Module()

        output_valid = Signal()
        output_data = Signal(64)
        packed_data = Signal(64)
        packed_count = Signal(range(8))
        output_fire = output_valid & self.o_stream.ready
        output_slot = ~output_valid | self.o_stream.ready
        input_fire = self.i_stream.valid & self.i_stream.ready
        m.d.comb += [
            self.i_stream.ready.eq(output_slot),
            self.o_stream.valid.eq(output_valid),
            self.o_stream.p.data.eq(output_data),
        ]

        with m.If(output_fire):
            m.d.sync += output_valid.eq(0)
        with m.If(input_fire):
            with m.If(packed_count == 7):
                m.d.sync += [
                    output_valid.eq(1),
                    output_data.eq(packed_data | (self.i_stream.payload << 56)),
                    packed_data.eq(0),
                    packed_count.eq(0),
                ]
            with m.Else():
                m.d.sync += packed_count.eq(packed_count + 1)
                with m.Switch(packed_count):
                    for byte_index in range(7):
                        with m.Case(byte_index):
                            m.d.sync += packed_data.word_select(byte_index, 8).eq(
                                self.i_stream.payload)

        return m


class HostReplyWordPacker(wiring.Component):
    """Pack decoded host-reply bytes into eight-byte SD-domain transfers.

    The nine-byte reply header is terminated with a one-byte word, then the even-sized block
    payload is packed into full words. Keeping the header boundary explicit lets the consumer
    change from metadata parsing to wide BRAM writes without carrying a byte across states.
    """

    i_stream: In(stream.Signature(data.StructLayout({"data": 8, "end": 1, "error": 1})))
    o_stream: Out(HOST_REPLY_WORD_STREAM_SIGNATURE)

    def elaborate(self, platform):
        m = Module()

        output_valid = Signal()
        output_data = Signal(64)
        output_count = Signal(4)
        output_end = Signal()
        output_error = Signal()
        packed_data = Signal(64)
        packed_count = Signal(range(9))
        in_payload = Signal()
        header_bytes = Signal(range(REPLY_SCHEMA.size + 1))
        pending_end = Signal()
        pending_error = Signal()

        output_fire = output_valid & self.o_stream.ready
        output_slot = ~output_valid | self.o_stream.ready
        input_fire = self.i_stream.valid & self.i_stream.ready
        m.d.comb += [
            self.i_stream.ready.eq(output_slot & ~pending_end),
            self.o_stream.valid.eq(output_valid),
            self.o_stream.p.data.eq(output_data),
            self.o_stream.p.count.eq(output_count),
            self.o_stream.p.end.eq(output_end),
            self.o_stream.p.error.eq(output_error),
        ]

        with m.If(output_fire):
            m.d.sync += output_valid.eq(0)

        # A malformed frame may end with a partial word. Emit it first and retain the delimiter
        # as a separate token so the downstream decoder can resynchronize.
        with m.If(output_slot & pending_end):
            m.d.sync += [
                output_valid.eq(1),
                output_data.eq(0),
                output_count.eq(0),
                output_end.eq(1),
                output_error.eq(pending_error),
                pending_end.eq(0),
                pending_error.eq(0),
                in_payload.eq(0),
                header_bytes.eq(0),
            ]
        with m.Elif(input_fire):
            with m.If(self.i_stream.p.end):
                with m.If(packed_count != 0):
                    m.d.sync += [
                        output_valid.eq(1),
                        output_data.eq(packed_data),
                        output_count.eq(packed_count),
                        output_end.eq(0),
                        output_error.eq(0),
                        packed_data.eq(0),
                        packed_count.eq(0),
                        pending_end.eq(1),
                        pending_error.eq(self.i_stream.p.error),
                    ]
                with m.Else():
                    m.d.sync += [
                        output_valid.eq(1),
                        output_data.eq(0),
                        output_count.eq(0),
                        output_end.eq(1),
                        output_error.eq(self.i_stream.p.error),
                        in_payload.eq(0),
                        header_bytes.eq(0),
                    ]
            with m.Elif(~in_payload & (header_bytes == REPLY_SCHEMA.size - 1)):
                # REPLY_SCHEMA is odd-sized; flush its final byte without waiting for payload.
                m.d.sync += [
                    output_valid.eq(1),
                    # The preceding eight header bytes were just flushed as one word, so the
                    # ninth byte always begins a fresh word.
                    output_data.eq(self.i_stream.p.data),
                    output_count.eq(packed_count + 1),
                    output_end.eq(0),
                    output_error.eq(0),
                    in_payload.eq(1),
                    header_bytes.eq(0),
                    packed_data.eq(0),
                    packed_count.eq(0),
                ]
            with m.Elif(packed_count == 7):
                m.d.sync += [
                    output_valid.eq(1),
                    output_data.eq(packed_data | (self.i_stream.p.data << 56)),
                    output_count.eq(8),
                    output_end.eq(0),
                    output_error.eq(0),
                    packed_data.eq(0),
                    packed_count.eq(0),
                ]
                with m.If(~in_payload):
                    m.d.sync += header_bytes.eq(header_bytes + 1)
            with m.Else():
                m.d.sync += [
                    packed_count.eq(packed_count + 1),
                ]
                with m.Switch(packed_count):
                    for byte_index in range(7):
                        with m.Case(byte_index):
                            m.d.sync += packed_data.word_select(byte_index, 8).eq(
                                self.i_stream.p.data)
                with m.If(~in_payload):
                    m.d.sync += header_bytes.eq(header_bytes + 1)

        return m
