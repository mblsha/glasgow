from amaranth import *
from amaranth.lib import data, enum, stream, wiring
from amaranth.lib.wiring import In, Out

from ._buffer import BlockBufferPortSignature
from ._gateware_support import (
    BLOCK_SIZE, BUFFER_BANK_BITS, IMAGE_SIZE_WIDTH, GatewareFrameSchema,
)
from ._interfaces import DataHostSignature
from ._protocol import (
    HOST_OPERATIONS, HOST_OPERATION_FOR_EVENT, HOST_OPERATION_FOR_REPLY,
    REQUEST_SCHEMA, REPLY_SCHEMA, BackendStatus, HostEvent, HostReply,
)
from ._reply_packer import HOST_REPLY_WORD_STREAM_SIGNATURE, RAW_BLOCK_WORD_STREAM_SIGNATURE


__all__ = ["HostBlockEngine"]


REQUEST_GATEWARE_SCHEMA = GatewareFrameSchema(REQUEST_SCHEMA)
REPLY_GATEWARE_SCHEMA = GatewareFrameSchema(REPLY_SCHEMA)


class TransactionPhase(enum.Enum, shape=2):
    Idle = 0
    Emit = 1
    AwaitReply = 2


class HostBlockEngine(wiring.Component):
    i_stream: In(HOST_REPLY_WORD_STREAM_SIGNATURE)
    read_i_stream: In(RAW_BLOCK_WORD_STREAM_SIGNATURE)
    o_stream: Out(stream.Signature(data.StructLayout({"data": 8, "end": 1})))

    generation: In(16)
    image_size: In(IMAGE_SIZE_WIDTH)
    current_address: In(32)
    erase_start: In(32)
    erase_end: In(32)
    buffer_write_blocked: In(1)

    storage: In(DataHostSignature())
    buffer: Out(BlockBufferPortSignature())

    protocol_errors: Out(16)
    backend_errors: Out(16)
    stale_replies: Out(16)
    max_host_service_latency: Out(32)
    transaction_phase: Out(2)
    reply_header_bytes: Out(4)
    reply_payload_bytes: Out(10)

    def elaborate(self, platform):
        m = Module()
        _elaborate_host_block_engine(
            m, self, self.i_stream, self.read_i_stream,
            self.o_stream, self.buffer, self.storage,
            self.current_address, self.erase_start, self.erase_end)
        return m


def _elaborate_host_block_engine(
        m, component, i_stream, read_i_stream, o_stream, block_buffer, storage,
        current_address, erase_start, erase_end):
    """Bridge block requests to framed, CRC-protected host messages.

    COBS encoding is applied outside this engine. The streams here contain decoded data tokens
    and a distinct end-of-frame token, so either side can discard one malformed frame and begin
    cleanly at the next delimiter.
    """

    self = component
    read = storage.read
    mutation = storage.mutation
    read_request = read.request
    write_request = mutation.write_request
    erase_request = mutation.erase_request

    # Host replies carry both a session generation and a request ID. This permits the SD clock
    # domains and USB pipes to be reset independently without ever accepting a reply left over
    # from the previous target-power cycle.
    read_buffer_ready = read.reply_ready
    read_buffer_address = read.reply_address
    read_buffer_consume = read.reply_consume
    read_reply_error = read.reply_error
    host_ack_valid = mutation.ack_valid
    host_ack_status = mutation.ack_status
    host_ack_consume = mutation.ack_consume
    transaction = Signal(data.StructLayout({
        "phase": TransactionPhase,
        "event": HostEvent,
        "reply": 8,
        "request_id": 16,
        "address": 32,
        "end_address": 32,
        "bank": BUFFER_BANK_BITS,
        "read_ahead": 1,
        "prefetch_only": 1,
    }))
    speculative_read_count = Signal(range(4))
    speculative_read_active = Signal()
    speculative_read_address = Signal(32)
    read_reply_published = Signal()

    reply_header = Signal(REPLY_GATEWARE_SCHEMA.layout)
    reply_header_count = Signal(range(REPLY_SCHEMA.size + 1))
    reply_count = Signal(range(BLOCK_SIZE + 1))
    raw_stale_block = Signal()
    m.d.comb += [
        self.transaction_phase.eq(transaction.phase),
        self.reply_header_bytes.eq(reply_header_count),
        self.reply_payload_bytes.eq(reply_count),
    ]

    reply_data = i_stream.p.data
    reply_bytes = i_stream.p.count
    reply_end = i_stream.p.end
    reply_error = i_stream.p.error
    reply_fire = i_stream.valid & i_stream.ready
    raw_read_fire = read_i_stream.valid & read_i_stream.ready
    raw_read_current = read_i_stream.p.generation == self.generation
    speculative_read_pending = speculative_read_count != 0
    speculative_read_matches = read.request_address == speculative_read_address
    discard_speculative_read = speculative_read_pending & (
        write_request | erase_request | (read_request & ~speculative_read_matches))

    reply_operation_known = Signal()
    reply_expected_length = Signal(16)
    with m.Switch(reply_header.opcode):
        for operation in HOST_OPERATIONS:
            with m.Case(operation.reply):
                m.d.comb += [
                    reply_operation_known.eq(1),
                    reply_expected_length.eq(operation.reply_payload.size),
                ]
    transaction_matches = \
        (transaction.phase == TransactionPhase.AwaitReply) & \
        (reply_header.opcode == transaction.reply) & \
        (reply_header.generation == self.generation) & \
        (reply_header.request_id == transaction.request_id)

    transaction_event_known = Signal()
    transaction_expected_reply = Signal(8)
    with m.Switch(transaction.event):
        for operation in HOST_OPERATIONS:
            with m.Case(operation.event):
                m.d.comb += [
                    transaction_event_known.eq(1),
                    transaction_expected_reply.eq(operation.reply),
                ]
    with m.If(transaction.phase != TransactionPhase.Idle):
        m.d.sd += [
            Assert(transaction_event_known),
            Assert(transaction.reply == transaction_expected_reply),
        ]

    m.d.sd += [
        read_reply_error.eq(0),
        read.request_accepted.eq(0),
        mutation.write_request_accepted.eq(0),
        mutation.erase_request_accepted.eq(0),
    ]
    with m.If(read_buffer_consume):
        m.d.sd += read_buffer_ready.eq(0)
    with m.If(host_ack_consume):
        m.d.sd += host_ack_valid.eq(0)

    m.d.comb += [
        i_stream.ready.eq(0),
        read_i_stream.ready.eq(0),
        block_buffer.write_en.eq(0),
        block_buffer.write_address.eq(0),
        block_buffer.write_data.eq(0),
    ]
    m.d.sd += [
        Assert(~(block_buffer.write_en.any() & block_buffer.read_active)),
        Assert(~(
            (read.request_accepted & mutation.write_request_accepted) |
            (read.request_accepted & mutation.erase_request_accepted) |
            (mutation.write_request_accepted & mutation.erase_request_accepted))),
    ]

    def discard_reply():
        m.d.sd += self.protocol_errors.eq(self.protocol_errors + 1)
        m.next = "Discard"

    def reject_ended_reply():
        m.d.sd += [
            self.protocol_errors.eq(self.protocol_errors + 1),
            reply_header_count.eq(0),
        ]
        m.next = "Header"

    with m.FSM(domain="sd", name="host_reply"):
        with m.State("Header"):
            with m.If(discard_speculative_read &
                      (transaction.phase == TransactionPhase.Idle)):
                # A two-block backend fetch may leave its second, sequential packet in the raw
                # pipe when CMD12 changes the target's next address. Drain that exactly one
                # packet before accepting the unrelated request; it must never be relabelled.
                m.d.sd += [reply_count.eq(0), raw_stale_block.eq(0)]
                m.next = "Raw-Discard"
            with m.Elif((transaction.phase == TransactionPhase.AwaitReply) &
                      (transaction.event == HostEvent.Read)):
                m.d.sd += [reply_count.eq(0), raw_stale_block.eq(0)]
                m.next = "Raw-Read"
            with m.Else():
                # Framed replies are retained for write/erase acknowledgements. Read data uses
                # a dedicated raw pipe so one complete block occupies one USB packet.
                m.d.comb += i_stream.ready.eq(
                    transaction.phase == TransactionPhase.AwaitReply)
                with m.If(reply_fire):
                    with m.If(reply_end):
                        reject_ended_reply()
                    with m.Elif((reply_header_count == 0) & (reply_bytes == 8)):
                        m.d.sd += [
                            reply_header.as_value()[:64].eq(reply_data),
                            reply_header_count.eq(8),
                        ]
                    with m.Elif((reply_header_count == 8) & (reply_bytes == 1)):
                        m.d.sd += [
                            reply_header.as_value()[64:72].eq(reply_data[:8]),
                            reply_header_count.eq(0),
                            reply_count.eq(0),
                        ]
                        m.next = "Header-Complete"
                    with m.Else():
                        reject_ended_reply()

        with m.State("Raw-Read"):
            m.d.comb += [
                # The SD data engine and host-fill path share one BRAM write
                # port. Backpressure the lossless raw FIFO while an incoming
                # SD write owns that port; consuming either payload here would
                # otherwise poison the block buffer at the first collision.
                read_i_stream.ready.eq(~self.buffer_write_blocked),
                block_buffer.write_en.eq(Mux(
                    raw_read_fire & raw_read_current & ~raw_stale_block,
                    C(0xff, 8), 0)),
                block_buffer.write_address.eq(Cat(reply_count[:9], transaction.bank)),
                block_buffer.write_data.eq(read_i_stream.p.data),
            ]
            with m.If(raw_read_fire):
                with m.If(raw_stale_block | ~raw_read_current):
                    with m.If(read_i_stream.p.last):
                        m.d.sd += [
                            raw_stale_block.eq(0),
                            reply_count.eq(0),
                            self.stale_replies.eq(self.stale_replies + 1),
                        ]
                    with m.Else():
                        m.d.sd += [
                            raw_stale_block.eq(1),
                            reply_count.eq(Mux(
                                reply_count == BLOCK_SIZE - 8,
                                0, reply_count + 8)),
                        ]
                with m.Elif((reply_count == BLOCK_SIZE - 8) &
                            read_i_stream.p.last):
                    m.d.sd += [
                        read_buffer_ready.eq(1),
                        read_buffer_address.eq(transaction.address),
                        read.reply_bank.eq(transaction.bank),
                        transaction.phase.eq(TransactionPhase.Idle),
                        reply_count.eq(0),
                    ]
                    with m.If(transaction.read_ahead):
                        m.d.sd += [
                            speculative_read_count.eq(0),
                            speculative_read_active.eq(1),
                            speculative_read_address.eq(transaction.address + BLOCK_SIZE),
                        ]
                    m.next = "Header"
                with m.Elif(read_i_stream.p.last |
                            (reply_count == BLOCK_SIZE - 8)):
                    m.d.sd += [
                        read_reply_error.eq(1),
                        transaction.phase.eq(TransactionPhase.Idle),
                        reply_count.eq(0),
                        self.protocol_errors.eq(self.protocol_errors + 1),
                    ]
                    m.next = "Header"
                with m.Else():
                    m.d.sd += reply_count.eq(reply_count + 8)

        with m.State("Raw-Discard"):
            m.d.comb += read_i_stream.ready.eq(1)
            with m.If(raw_read_fire):
                with m.If(raw_stale_block | ~raw_read_current):
                    with m.If(read_i_stream.p.last):
                        m.d.sd += [
                            raw_stale_block.eq(0),
                            reply_count.eq(0),
                            self.stale_replies.eq(self.stale_replies + 1),
                        ]
                    with m.Else():
                        m.d.sd += [
                            raw_stale_block.eq(1),
                            reply_count.eq(Mux(
                                reply_count == BLOCK_SIZE - 8,
                                0, reply_count + 8)),
                        ]
                with m.Elif(read_i_stream.p.last):
                    m.d.sd += [
                        reply_count.eq(0),
                        self.protocol_errors.eq(
                            self.protocol_errors +
                            (reply_count != BLOCK_SIZE - 8)),
                    ]
                    with m.If(speculative_read_count == 1):
                        m.d.sd += speculative_read_count.eq(0)
                        m.next = "Header"
                    with m.Else():
                        m.d.sd += [
                            speculative_read_count.eq(speculative_read_count - 1),
                            speculative_read_address.eq(
                                speculative_read_address + BLOCK_SIZE),
                        ]
                with m.Elif(reply_count == BLOCK_SIZE - 8):
                    m.d.sd += [
                        reply_count.eq(0),
                        self.protocol_errors.eq(self.protocol_errors + 1),
                    ]
                with m.Else():
                    m.d.sd += reply_count.eq(reply_count + 8)

        with m.State("Header-Complete"):
            with m.If(reply_header.payload_length != 0):
                discard_reply()
            with m.Else():
                m.next = "End"

        with m.State("End"):
            m.d.comb += i_stream.ready.eq(1)
            with m.If(reply_fire):
                with m.If(~reply_end):
                    discard_reply()
                with m.Else():
                    common_valid = REPLY_GATEWARE_SCHEMA.constants_valid(reply_header) & ~reply_error
                    with m.If(~common_valid):
                        m.d.sd += self.protocol_errors.eq(self.protocol_errors + 1)
                        with m.If(read_reply_published):
                            m.d.sd += [
                                read_reply_error.eq(1),
                                transaction.phase.eq(TransactionPhase.Idle),
                            ]
                    with m.Elif(~reply_operation_known):
                        m.d.sd += self.protocol_errors.eq(self.protocol_errors + 1)
                    with m.Elif(reply_header.payload_length != reply_expected_length):
                        m.d.sd += self.protocol_errors.eq(self.protocol_errors + 1)
                    with m.Elif(~transaction_matches):
                        m.d.sd += self.stale_replies.eq(self.stale_replies + 1)
                    with m.Else():
                        m.d.sd += transaction.phase.eq(TransactionPhase.Idle)
                        with m.If(transaction.event == HostEvent.Read):
                            m.d.sd += read.reply_bank.eq(transaction.bank)
                            with m.If(reply_header.status == BackendStatus.OK):
                                with m.If(~read_reply_published):
                                    m.d.sd += [
                                        read_buffer_ready.eq(1),
                                        read_buffer_address.eq(transaction.address),
                                    ]
                            with m.Else():
                                m.d.sd += [
                                    read_reply_error.eq(1),
                                    self.backend_errors.eq(self.backend_errors + 1),
                                ]
                            with m.If(transaction.read_ahead):
                                m.d.sd += [
                                    speculative_read_count.eq(0),
                                    speculative_read_active.eq(1),
                                    speculative_read_address.eq(
                                        transaction.address + BLOCK_SIZE),
                                ]
                        with m.Else():
                            m.d.sd += [
                                host_ack_status.eq(reply_header.status),
                                host_ack_valid.eq(1),
                            ]
                            with m.If(reply_header.status != BackendStatus.OK):
                                m.d.sd += self.backend_errors.eq(self.backend_errors + 1)
                    m.d.sd += [
                        reply_header_count.eq(0),
                        read_reply_published.eq(0),
                    ]
                    m.next = "Header"

        with m.State("Discard"):
            m.d.comb += i_stream.ready.eq(1)
            with m.If(reply_fire & reply_end):
                m.d.sd += [
                    reply_header_count.eq(0),
                ]
                m.next = "Header"

    next_request = Signal(16, init=1)
    event_count = Signal(range(BLOCK_SIZE + 1))
    event_read_address = Signal(range(BLOCK_SIZE))
    event_read_advance = Signal()

    with m.If(read_request & (transaction.phase == TransactionPhase.Idle) &
              (~speculative_read_pending | speculative_read_matches)):
        m.d.sd += [
            transaction.phase.eq(Mux(
                speculative_read_pending,
                TransactionPhase.AwaitReply, TransactionPhase.Emit)),
            transaction.event.eq(HostEvent.Read),
            transaction.reply.eq(HOST_OPERATION_FOR_EVENT[HostEvent.Read].reply),
            transaction.request_id.eq(next_request),
            transaction.address.eq(read.request_address),
            transaction.bank.eq(read.request_bank),
            transaction.prefetch_only.eq(0),
            transaction.read_ahead.eq(
                ~speculative_read_pending & (read.request_blocks == 2)),
            next_request.eq(next_request + 1),
            read.request_accepted.eq(1),
        ]
        with m.If(speculative_read_pending):
            m.d.sd += [
                speculative_read_count.eq(speculative_read_count - 1),
                speculative_read_address.eq(speculative_read_address + BLOCK_SIZE),
            ]
    with m.Elif(write_request & (transaction.phase == TransactionPhase.Idle) &
                ~speculative_read_pending):
        m.d.sd += [
            transaction.phase.eq(TransactionPhase.Emit),
            transaction.event.eq(HostEvent.Write),
            transaction.reply.eq(HOST_OPERATION_FOR_EVENT[HostEvent.Write].reply),
            transaction.request_id.eq(next_request),
            transaction.address.eq(current_address),
            transaction.bank.eq(mutation.write_bank),
            transaction.prefetch_only.eq(0),
            next_request.eq(next_request + 1),
            mutation.write_request_accepted.eq(1),
            speculative_read_active.eq(0),
        ]
    with m.Elif(erase_request & (transaction.phase == TransactionPhase.Idle) &
                ~speculative_read_pending):
        m.d.sd += [
            transaction.phase.eq(TransactionPhase.Emit),
            transaction.event.eq(HostEvent.Erase),
            transaction.reply.eq(HOST_OPERATION_FOR_EVENT[HostEvent.Erase].reply),
            transaction.request_id.eq(next_request),
            transaction.address.eq(erase_start),
            transaction.end_address.eq(erase_end),
            transaction.prefetch_only.eq(0),
            next_request.eq(next_request + 1),
            mutation.erase_request_accepted.eq(1),
            speculative_read_active.eq(0),
        ]
    with m.Elif(speculative_read_active & (speculative_read_count == 0) &
                (transaction.phase == TransactionPhase.Idle) &
                (speculative_read_address +
                 (speculative_read_count + 1) * BLOCK_SIZE <= self.image_size)):
        # Refill the sequential raw stream before its final packet is consumed. This request has
        # no BRAM bank yet; its one complete USB packet is address-accounted below and remains in
        # the raw FIFO until the data engine reserves its bank.
        m.d.sd += [
            transaction.phase.eq(TransactionPhase.Emit),
            transaction.event.eq(HostEvent.Read),
            transaction.reply.eq(HOST_OPERATION_FOR_EVENT[HostEvent.Read].reply),
            transaction.request_id.eq(next_request),
            transaction.address.eq(
                speculative_read_address + speculative_read_count * BLOCK_SIZE),
            transaction.read_ahead.eq(0),
            transaction.prefetch_only.eq(1),
            next_request.eq(next_request + 1),
        ]

    event_payload_length = Signal(16)
    with m.Switch(transaction.event):
        for operation in HOST_OPERATIONS:
            with m.Case(operation.event):
                m.d.comb += event_payload_length.eq(operation.request_payload.size)

    event_header_view = Signal(REQUEST_GATEWARE_SCHEMA.layout)
    m.d.comb += [
        event_header_view.opcode.eq(transaction.event),
        event_header_view.generation.eq(self.generation),
        event_header_view.request_id.eq(transaction.request_id),
        event_header_view.payload_length.eq(event_payload_length),
    ]
    event_header = Array(REQUEST_GATEWARE_SCHEMA.bytes(event_header_view))

    m.d.comb += [
        o_stream.valid.eq(0),
        o_stream.p.data.eq(0),
        o_stream.p.end.eq(0),
        event_read_advance.eq(0),
        block_buffer.read_active.eq(
            (transaction.phase == TransactionPhase.Emit) &
            (transaction.event == HostEvent.Write)),
        block_buffer.read_address.eq(
            Cat(Mux(event_read_advance, event_count + 1, event_read_address)[:9],
                transaction.bank)),
    ]
    event_fire = o_stream.valid & o_stream.ready

    def send_event_byte(value):
        m.d.comb += [
            o_stream.valid.eq(1),
            o_stream.p.data.eq(value),
        ]

    with m.FSM(domain="sd", name="host_event"):
        with m.State("Idle"):
            with m.If(transaction.phase == TransactionPhase.Emit):
                m.d.sd += event_count.eq(0)
                m.next = "Header"

        with m.State("Header"):
            send_event_byte(event_header[event_count])
            with m.If(event_fire):
                with m.If(event_count == REQUEST_SCHEMA.size - 1):
                    m.d.sd += event_count.eq(0)
                    m.next = "Address"
                with m.Else():
                    m.d.sd += event_count.eq(event_count + 1)

        with m.State("Address"):
            send_event_byte(transaction.address.word_select(event_count, 8))
            with m.If(event_fire):
                with m.If(event_count == 3):
                    m.d.sd += event_count.eq(0)
                    with m.Switch(transaction.event):
                        with m.Case(HostEvent.Read):
                            m.next = "Read-Blocks"
                        with m.Case(HostEvent.Write):
                            m.d.sd += event_read_address.eq(0)
                            m.next = "Write-Prefetch"
                        with m.Case(HostEvent.Erase):
                            m.next = "Erase-End"
                with m.Else():
                    m.d.sd += event_count.eq(event_count + 1)

        with m.State("Read-Blocks"):
            # Every backend response is exactly one 512-byte USB packet. read_ahead describes
            # sequential intent inside the FPGA; it does not widen the host transaction.
            send_event_byte(1)
            with m.If(event_fire):
                m.next = "End"

        with m.State("Write-Prefetch"):
            m.next = "Write-Data"

        with m.State("Write-Data"):
            send_event_byte(block_buffer.read_data)
            with m.If(event_fire):
                with m.If(event_count == BLOCK_SIZE - 1):
                    m.next = "End"
                with m.Else():
                    m.d.comb += event_read_advance.eq(1)
                    m.d.sd += [
                        event_count.eq(event_count + 1),
                        event_read_address.eq(event_count + 1),
                    ]

        with m.State("Erase-End"):
            send_event_byte(transaction.end_address.word_select(event_count, 8))
            with m.If(event_fire):
                with m.If(event_count == 3):
                    m.next = "End"
                with m.Else():
                    m.d.sd += event_count.eq(event_count + 1)

        with m.State("End"):
            m.d.comb += [
                o_stream.valid.eq(1),
                o_stream.p.end.eq(1),
            ]
            with m.If(event_fire):
                with m.If(transaction.prefetch_only):
                    m.d.sd += [
                        transaction.phase.eq(TransactionPhase.Idle),
                        speculative_read_count.eq(speculative_read_count + 1),
                    ]
                with m.Else():
                    m.d.sd += transaction.phase.eq(TransactionPhase.AwaitReply)
                m.next = "Idle"
