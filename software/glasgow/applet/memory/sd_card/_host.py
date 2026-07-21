from amaranth import *
from amaranth.lib import data, stream, wiring
from amaranth.lib.wiring import In, Out

from ._buffer import BlockBufferPortSignature
from ._gateware_support import BLOCK_SIZE, GatewareFrameSchema
from ._interfaces import DataHostSignature
from ._protocol import (
    HOST_EVENT_PAYLOAD_LENGTH, HOST_REPLY_PAYLOAD_LENGTH,
    REQUEST_SCHEMA, REPLY_SCHEMA, BackendStatus, HostEvent, HostReply,
)


__all__ = ["HostBlockEngine"]


REQUEST_GATEWARE_SCHEMA = GatewareFrameSchema(REQUEST_SCHEMA)
REPLY_GATEWARE_SCHEMA = GatewareFrameSchema(REPLY_SCHEMA)


class HostBlockEngine(wiring.Component):
    i_stream: In(stream.Signature(data.StructLayout({"data": 8, "end": 1, "error": 1})))
    o_stream: Out(stream.Signature(data.StructLayout({"data": 8, "end": 1})))

    generation: In(16)
    current_address: In(32)
    erase_start: In(32)
    erase_end: In(32)

    storage: In(DataHostSignature())
    buffer: Out(BlockBufferPortSignature())

    protocol_errors: Out(16)
    backend_errors: Out(16)
    stale_replies: Out(16)

    def elaborate(self, platform):
        m = Module()
        _elaborate_host_block_engine(
            m, self, self.i_stream, self.o_stream, self.buffer, self.storage,
            self.current_address, self.erase_start, self.erase_end)
        return m


def _elaborate_host_block_engine(
        m, component, i_stream, o_stream, block_buffer, storage,
        current_address, erase_start, erase_end):
    """Bridge block requests to framed, CRC-protected host messages.

    COBS encoding is applied outside this engine. The streams here contain decoded data tokens
    and a distinct end-of-frame token, so either side can discard one malformed frame and begin
    cleanly at the next delimiter.
    """

    self = component
    read_request = storage.read_request
    write_request = storage.write_request
    erase_request = storage.erase_request

    # Host replies carry both a session generation and a request ID. This permits the SD clock
    # domains and USB pipes to be reset independently without ever accepting a reply left over
    # from the previous target-power cycle.
    read_buffer_ready = storage.read_buffer_ready
    read_buffer_address = storage.read_buffer_address
    read_buffer_consume = storage.read_buffer_consume
    read_in_flight = Signal()
    read_in_flight_address = Signal(32)
    read_in_flight_request = Signal(16)
    read_reply_error = storage.read_reply_error
    host_ack_valid = storage.host_ack_valid
    host_ack_status = storage.host_ack_status
    host_ack_consume = storage.host_ack_consume
    ack_in_flight = Signal()
    ack_in_flight_opcode = Signal(8)
    ack_in_flight_request = Signal(16)

    reply_header = Signal(REPLY_GATEWARE_SCHEMA.layout)
    reply_header_count = Signal(range(REPLY_SCHEMA.size))
    reply_count = Signal(range(BLOCK_SIZE + 1))

    reply_data = i_stream.p.data
    reply_end = i_stream.p.end
    reply_error = i_stream.p.error
    reply_fire = i_stream.valid & i_stream.ready

    m.d.sd += [
        read_reply_error.eq(0),
        storage.read_request_accepted.eq(0),
        storage.write_request_accepted.eq(0),
        storage.erase_request_accepted.eq(0),
    ]
    with m.If(read_buffer_consume):
        m.d.sd += read_buffer_ready.eq(0)
    with m.If(host_ack_consume):
        m.d.sd += host_ack_valid.eq(0)

    m.d.comb += [
        i_stream.ready.eq(0),
        block_buffer.write_en.eq(0),
        block_buffer.write_address.eq(0),
        block_buffer.write_data.eq(0),
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
            m.d.comb += i_stream.ready.eq(1)
            with m.If(reply_fire):
                with m.If(reply_end):
                    reject_ended_reply()
                with m.Else():
                    REPLY_GATEWARE_SCHEMA.load_byte(
                        m, reply_header, reply_header_count, reply_data, domain="sd")

                    with m.If(reply_header_count == REPLY_SCHEMA.size - 1):
                        m.d.sd += [
                            reply_header_count.eq(0),
                            reply_count.eq(0),
                        ]
                        m.next = "Header-Complete"
                    with m.Else():
                        m.d.sd += reply_header_count.eq(reply_header_count + 1)

        with m.State("Header-Complete"):
            with m.If(reply_header.payload_length > BLOCK_SIZE):
                discard_reply()
            with m.Elif(reply_header.payload_length == 0):
                m.next = "End"
            with m.Else():
                m.next = "Payload"

        with m.State("Payload"):
            m.d.comb += [
                i_stream.ready.eq(1),
                block_buffer.write_en.eq(reply_fire & ~reply_end &
                    (reply_header.opcode == HostReply.ReadData) &
                    (reply_header.status == BackendStatus.OK) &
                    (reply_header.payload_length == BLOCK_SIZE) &
                    read_in_flight &
                    (reply_header.generation == self.generation) &
                    (reply_header.request_id == read_in_flight_request)),
                block_buffer.write_address.eq(reply_count),
                block_buffer.write_data.eq(reply_data),
            ]
            with m.If(reply_fire):
                with m.If(reply_end):
                    reject_ended_reply()
                with m.Else():
                    with m.If(reply_count == reply_header.payload_length - 1):
                        m.next = "End"
                    with m.Else():
                        m.d.sd += reply_count.eq(reply_count + 1)

        with m.State("End"):
            m.d.comb += i_stream.ready.eq(1)
            with m.If(reply_fire):
                with m.If(~reply_end):
                    discard_reply()
                with m.Else():
                    common_valid = REPLY_GATEWARE_SCHEMA.constants_valid(reply_header) & ~reply_error
                    with m.If(~common_valid):
                        m.d.sd += self.protocol_errors.eq(self.protocol_errors + 1)
                    with m.Elif(reply_header.opcode == HostReply.ReadData):
                        read_matches = read_in_flight & \
                            (reply_header.generation == self.generation) & \
                            (reply_header.request_id == read_in_flight_request)
                        with m.If(reply_header.payload_length !=
                                  HOST_REPLY_PAYLOAD_LENGTH[HostReply.ReadData]):
                            m.d.sd += self.protocol_errors.eq(self.protocol_errors + 1)
                        with m.Elif(read_matches):
                            m.d.sd += read_in_flight.eq(0)
                            with m.If(reply_header.status == BackendStatus.OK):
                                m.d.sd += [
                                    read_buffer_ready.eq(1),
                                    read_buffer_address.eq(read_in_flight_address),
                                ]
                            with m.Else():
                                m.d.sd += [
                                    read_reply_error.eq(1),
                                    self.backend_errors.eq(self.backend_errors + 1),
                                ]
                        with m.Else():
                            m.d.sd += self.stale_replies.eq(self.stale_replies + 1)
                    with m.Elif((reply_header.opcode == HostReply.WriteAck) |
                                (reply_header.opcode == HostReply.EraseAck)):
                        ack_matches = ack_in_flight & \
                            (reply_header.opcode == ack_in_flight_opcode) & \
                            (reply_header.generation == self.generation) & \
                            (reply_header.request_id == ack_in_flight_request)
                        with m.If(reply_header.payload_length != 0):
                            m.d.sd += self.protocol_errors.eq(self.protocol_errors + 1)
                        with m.Elif(ack_matches):
                            m.d.sd += [
                                host_ack_status.eq(reply_header.status),
                                host_ack_valid.eq(1),
                                ack_in_flight.eq(0),
                            ]
                            with m.If(reply_header.status != BackendStatus.OK):
                                m.d.sd += self.backend_errors.eq(self.backend_errors + 1)
                        with m.Else():
                            m.d.sd += self.stale_replies.eq(self.stale_replies + 1)
                    with m.Else():
                        m.d.sd += self.protocol_errors.eq(self.protocol_errors + 1)
                    m.d.sd += [
                        reply_header_count.eq(0),
                    ]
                    m.next = "Header"

        with m.State("Discard"):
            m.d.comb += i_stream.ready.eq(1)
            with m.If(reply_fire & reply_end):
                m.d.sd += [
                    reply_header_count.eq(0),
                ]
                m.next = "Header"

    event_address = Signal(32)
    event_request = Signal(16)
    next_request = Signal(16, init=1)
    event_count = Signal(range(BLOCK_SIZE + 1))
    event_read_address = Signal(range(BLOCK_SIZE))
    event_read_advance = Signal()
    event_kind = Signal(HostEvent, init=HostEvent.Read)
    event_pending = Signal()

    with m.If(read_request & ~event_pending & ~read_in_flight):
        m.d.sd += [
            event_kind.eq(HostEvent.Read),
            event_address.eq(current_address),
            event_request.eq(next_request),
            next_request.eq(next_request + 1),
            read_in_flight_address.eq(current_address),
            read_in_flight_request.eq(next_request),
            event_pending.eq(1),
            storage.read_request_accepted.eq(1),
        ]
    with m.Elif(write_request & ~event_pending & ~ack_in_flight):
        m.d.sd += [
            event_kind.eq(HostEvent.Write),
            event_address.eq(current_address),
            event_request.eq(next_request),
            next_request.eq(next_request + 1),
            ack_in_flight.eq(1),
            ack_in_flight_opcode.eq(HostReply.WriteAck),
            ack_in_flight_request.eq(next_request),
            event_pending.eq(1),
            storage.write_request_accepted.eq(1),
        ]
    with m.Elif(erase_request & ~event_pending & ~ack_in_flight):
        m.d.sd += [
            event_kind.eq(HostEvent.Erase),
            event_address.eq(erase_start),
            event_request.eq(next_request),
            next_request.eq(next_request + 1),
            ack_in_flight.eq(1),
            ack_in_flight_opcode.eq(HostReply.EraseAck),
            ack_in_flight_request.eq(next_request),
            event_pending.eq(1),
            storage.erase_request_accepted.eq(1),
        ]

    event_payload_length = Signal(16)
    with m.Switch(event_kind):
        for opcode, payload_length in HOST_EVENT_PAYLOAD_LENGTH.items():
            with m.Case(opcode):
                m.d.comb += event_payload_length.eq(payload_length)

    event_header_view = Signal(REQUEST_GATEWARE_SCHEMA.layout)
    m.d.comb += [
        event_header_view.opcode.eq(event_kind),
        event_header_view.generation.eq(self.generation),
        event_header_view.request_id.eq(event_request),
        event_header_view.payload_length.eq(event_payload_length),
    ]
    event_header = Array(REQUEST_GATEWARE_SCHEMA.bytes(event_header_view))

    m.d.comb += [
        o_stream.valid.eq(0),
        o_stream.p.data.eq(0),
        o_stream.p.end.eq(0),
        event_read_advance.eq(0),
        block_buffer.read_active.eq(event_pending & (event_kind == HostEvent.Write)),
        block_buffer.read_address.eq(
            Mux(event_read_advance, event_count + 1, event_read_address)),
    ]
    event_fire = o_stream.valid & o_stream.ready

    def send_event_byte(value):
        m.d.comb += [
            o_stream.valid.eq(1),
            o_stream.p.data.eq(value),
        ]

    with m.FSM(domain="sd", name="host_event"):
        with m.State("Idle"):
            with m.If(event_pending):
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
            send_event_byte(event_address.word_select(event_count, 8))
            with m.If(event_fire):
                with m.If(event_count == 3):
                    m.d.sd += event_count.eq(0)
                    with m.Switch(event_kind):
                        with m.Case(HostEvent.Read):
                            m.next = "End"
                        with m.Case(HostEvent.Write):
                            m.d.sd += event_read_address.eq(0)
                            m.next = "Write-Prefetch"
                        with m.Case(HostEvent.Erase):
                            m.next = "Erase-End"
                with m.Else():
                    m.d.sd += event_count.eq(event_count + 1)

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
            send_event_byte(erase_end.word_select(event_count, 8))
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
                m.d.sd += event_pending.eq(0)
                with m.If(event_kind == HostEvent.Read):
                    m.d.sd += read_in_flight.eq(1)
                m.next = "Idle"
