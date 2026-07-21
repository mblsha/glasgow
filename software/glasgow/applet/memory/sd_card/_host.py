from amaranth import *

from ._gateware_support import BLOCK_SIZE, crc16_byte_value
from ._protocol import (
    FRAME_VERSION, BackendStatus, HostEvent, HostReply, ReplyHeader, RequestHeader,
)


def elaborate_host_block_engine(
        m, component, host_to_sd, sd_to_host, block_write, block_read, core):
    """Bridge block requests to framed, CRC-protected host messages.

    COBS encoding is applied outside this engine. The streams here contain decoded data tokens
    and a distinct end-of-frame token, so either side can discard one malformed frame and begin
    cleanly at the next delimiter.
    """

    self = component
    current_address = core.current_address
    erase_start = core.erase_start
    erase_end = core.erase_end
    read_request = core.read_request
    write_request = core.write_request
    erase_request = core.erase_request

    # Host replies carry both a session generation and a request ID. This permits the SD clock
    # domains and USB pipes to be reset independently without ever accepting a reply left over
    # from the previous target-power cycle.
    read_buffer_ready = core.read_buffer_ready
    read_buffer_address = core.read_buffer_address
    read_buffer_consume = core.read_buffer_consume
    read_in_flight = Signal()
    read_in_flight_address = Signal(32)
    read_in_flight_request = Signal(16)
    read_reply_error = core.read_reply_error
    host_ack_valid = core.host_ack_valid
    host_ack_status = core.host_ack_status
    host_ack_consume = core.host_ack_consume
    ack_in_flight = Signal()
    ack_in_flight_opcode = Signal(8)
    ack_in_flight_request = Signal(16)

    reply_version = Signal(8)
    reply_opcode = Signal(8)
    reply_generation = Signal(16)
    reply_request = Signal(16)
    reply_status = Signal(8)
    reply_length = Signal(16)
    reply_header_count = Signal(range(ReplyHeader.size))
    reply_count = Signal(range(BLOCK_SIZE + 1))
    reply_crc = Signal(16)
    reply_received_crc = Signal(16)

    reply_data = host_to_sd.o.p.data
    reply_end = host_to_sd.o.p.end
    reply_fire = host_to_sd.o.valid & host_to_sd.o.ready

    m.d.sd += [
        read_reply_error.eq(0),
        core.read_request_accepted.eq(0),
        core.write_request_accepted.eq(0),
        core.erase_request_accepted.eq(0),
    ]
    with m.If(read_buffer_consume):
        m.d.sd += read_buffer_ready.eq(0)
    with m.If(host_ack_consume):
        m.d.sd += host_ack_valid.eq(0)

    m.d.comb += [
        host_to_sd.o.ready.eq(0),
        block_write.en.eq(0),
        block_write.addr.eq(0),
        block_write.data.eq(0),
    ]

    def discard_reply():
        m.d.sd += self.protocol_errors.eq(self.protocol_errors + 1)
        m.next = "Discard"

    def reject_ended_reply():
        m.d.sd += [
            self.protocol_errors.eq(self.protocol_errors + 1),
            reply_crc.eq(0),
            reply_header_count.eq(0),
        ]
        m.next = "Header"

    with m.FSM(domain="sd", name="host_reply"):
        with m.State("Header"):
            m.d.comb += host_to_sd.o.ready.eq(1)
            with m.If(reply_fire):
                with m.If(reply_end):
                    reject_ended_reply()
                with m.Else():
                    m.d.sd += reply_crc.eq(crc16_byte_value(reply_crc, reply_data))
                    with m.Switch(reply_header_count):
                        with m.Case(0):
                            m.d.sd += reply_version.eq(reply_data)
                        with m.Case(1):
                            m.d.sd += reply_opcode.eq(reply_data)
                        with m.Case(2):
                            m.d.sd += reply_generation[:8].eq(reply_data)
                        with m.Case(3):
                            m.d.sd += reply_generation[8:].eq(reply_data)
                        with m.Case(4):
                            m.d.sd += reply_request[:8].eq(reply_data)
                        with m.Case(5):
                            m.d.sd += reply_request[8:].eq(reply_data)
                        with m.Case(6):
                            m.d.sd += reply_status.eq(reply_data)
                        with m.Case(7):
                            m.d.sd += reply_length[:8].eq(reply_data)
                        with m.Case(8):
                            m.d.sd += reply_length[8:].eq(reply_data)

                    with m.If(reply_header_count == ReplyHeader.size - 1):
                        complete_length = Cat(reply_length[:8], reply_data)
                        m.d.sd += [
                            reply_header_count.eq(0),
                            reply_count.eq(0),
                        ]
                        with m.If(complete_length > BLOCK_SIZE):
                            discard_reply()
                        with m.Elif(complete_length == 0):
                            m.next = "CRC-Low"
                        with m.Else():
                            m.next = "Payload"
                    with m.Else():
                        m.d.sd += reply_header_count.eq(reply_header_count + 1)

        with m.State("Payload"):
            m.d.comb += [
                host_to_sd.o.ready.eq(1),
                block_write.en.eq(reply_fire & ~reply_end &
                    (reply_opcode == HostReply.ReadData) &
                    (reply_status == BackendStatus.OK) &
                    (reply_length == BLOCK_SIZE) &
                    read_in_flight &
                    (reply_generation == self.generation) &
                    (reply_request == read_in_flight_request)),
                block_write.addr.eq(reply_count),
                block_write.data.eq(reply_data),
            ]
            with m.If(reply_fire):
                with m.If(reply_end):
                    reject_ended_reply()
                with m.Else():
                    m.d.sd += reply_crc.eq(crc16_byte_value(reply_crc, reply_data))
                    with m.If(reply_count == reply_length - 1):
                        m.next = "CRC-Low"
                    with m.Else():
                        m.d.sd += reply_count.eq(reply_count + 1)

        with m.State("CRC-Low"):
            m.d.comb += host_to_sd.o.ready.eq(1)
            with m.If(reply_fire):
                with m.If(reply_end):
                    reject_ended_reply()
                with m.Else():
                    m.d.sd += reply_received_crc[:8].eq(reply_data)
                    m.next = "CRC-High"

        with m.State("CRC-High"):
            m.d.comb += host_to_sd.o.ready.eq(1)
            with m.If(reply_fire):
                with m.If(reply_end):
                    reject_ended_reply()
                with m.Else():
                    m.d.sd += reply_received_crc[8:].eq(reply_data)
                    m.next = "End"

        with m.State("End"):
            m.d.comb += host_to_sd.o.ready.eq(1)
            with m.If(reply_fire):
                with m.If(~reply_end):
                    discard_reply()
                with m.Else():
                    crc_matches = reply_received_crc == reply_crc
                    common_valid = (reply_version == FRAME_VERSION) & crc_matches
                    with m.If(~common_valid):
                        m.d.sd += self.protocol_errors.eq(self.protocol_errors + 1)
                    with m.Elif(reply_opcode == HostReply.ReadData):
                        read_matches = read_in_flight & \
                            (reply_generation == self.generation) & \
                            (reply_request == read_in_flight_request)
                        with m.If(reply_length != BLOCK_SIZE):
                            m.d.sd += self.protocol_errors.eq(self.protocol_errors + 1)
                        with m.Elif(read_matches):
                            m.d.sd += read_in_flight.eq(0)
                            with m.If(reply_status == BackendStatus.OK):
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
                    with m.Elif((reply_opcode == HostReply.WriteAck) |
                                (reply_opcode == HostReply.EraseAck)):
                        ack_matches = ack_in_flight & \
                            (reply_opcode == ack_in_flight_opcode) & \
                            (reply_generation == self.generation) & \
                            (reply_request == ack_in_flight_request)
                        with m.If(reply_length != 0):
                            m.d.sd += self.protocol_errors.eq(self.protocol_errors + 1)
                        with m.Elif(ack_matches):
                            m.d.sd += [
                                host_ack_status.eq(reply_status),
                                host_ack_valid.eq(1),
                                ack_in_flight.eq(0),
                            ]
                            with m.If(reply_status != BackendStatus.OK):
                                m.d.sd += self.backend_errors.eq(self.backend_errors + 1)
                        with m.Else():
                            m.d.sd += self.stale_replies.eq(self.stale_replies + 1)
                    with m.Else():
                        m.d.sd += self.protocol_errors.eq(self.protocol_errors + 1)
                    m.d.sd += [
                        reply_crc.eq(0),
                        reply_header_count.eq(0),
                    ]
                    m.next = "Header"

        with m.State("Discard"):
            m.d.comb += host_to_sd.o.ready.eq(1)
            with m.If(reply_fire & reply_end):
                m.d.sd += [
                    reply_crc.eq(0),
                    reply_header_count.eq(0),
                ]
                m.next = "Header"

    event_address = Signal(32)
    event_request = Signal(16)
    next_request = Signal(16, init=1)
    event_count = core.event_count
    event_read_address = core.event_read_address
    event_read_advance = core.event_read_advance
    event_kind = core.event_kind
    event_pending = core.event_pending
    event_crc = Signal(16)

    with m.If(read_request & ~event_pending & ~read_in_flight):
        m.d.sd += [
            event_kind.eq(HostEvent.Read),
            event_address.eq(current_address),
            event_request.eq(next_request),
            next_request.eq(next_request + 1),
            read_in_flight_address.eq(current_address),
            read_in_flight_request.eq(next_request),
            event_pending.eq(1),
            core.read_request_accepted.eq(1),
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
            core.write_request_accepted.eq(1),
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
            core.erase_request_accepted.eq(1),
        ]

    event_payload_length = Signal(16)
    with m.Switch(event_kind):
        with m.Case(HostEvent.Read):
            m.d.comb += event_payload_length.eq(4)
        with m.Case(HostEvent.Write):
            m.d.comb += event_payload_length.eq(4 + BLOCK_SIZE)
        with m.Case(HostEvent.Erase):
            m.d.comb += event_payload_length.eq(8)

    event_header = Array([
        Const(FRAME_VERSION, 8),
        Cat(event_kind, C(0, 8 - len(event_kind))),
        self.generation[:8], self.generation[8:],
        event_request[:8], event_request[8:],
        event_payload_length[:8], event_payload_length[8:],
    ])

    m.d.comb += [
        sd_to_host.i.valid.eq(0),
        sd_to_host.i.p.data.eq(0),
        sd_to_host.i.p.end.eq(0),
        event_read_advance.eq(0),
    ]
    event_fire = sd_to_host.i.valid & sd_to_host.i.ready

    def send_event_byte(value):
        m.d.comb += [
            sd_to_host.i.valid.eq(1),
            sd_to_host.i.p.data.eq(value),
        ]

    def advance_event_crc(value):
        m.d.sd += event_crc.eq(crc16_byte_value(event_crc, value))

    with m.FSM(domain="sd", name="host_event"):
        with m.State("Idle"):
            with m.If(event_pending):
                m.d.sd += [event_count.eq(0), event_crc.eq(0)]
                m.next = "Header"

        with m.State("Header"):
            send_event_byte(event_header[event_count])
            with m.If(event_fire):
                advance_event_crc(event_header[event_count])
                with m.If(event_count == RequestHeader.size - 1):
                    m.d.sd += event_count.eq(0)
                    m.next = "Address"
                with m.Else():
                    m.d.sd += event_count.eq(event_count + 1)

        with m.State("Address"):
            send_event_byte(event_address.word_select(event_count, 8))
            with m.If(event_fire):
                advance_event_crc(event_address.word_select(event_count, 8))
                with m.If(event_count == 3):
                    m.d.sd += event_count.eq(0)
                    with m.Switch(event_kind):
                        with m.Case(HostEvent.Read):
                            m.next = "CRC-Low"
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
            send_event_byte(block_read.data)
            with m.If(event_fire):
                advance_event_crc(block_read.data)
                with m.If(event_count == BLOCK_SIZE - 1):
                    m.next = "CRC-Low"
                with m.Else():
                    m.d.comb += event_read_advance.eq(1)
                    m.d.sd += [
                        event_count.eq(event_count + 1),
                        event_read_address.eq(event_count + 1),
                    ]

        with m.State("Erase-End"):
            send_event_byte(erase_end.word_select(event_count, 8))
            with m.If(event_fire):
                advance_event_crc(erase_end.word_select(event_count, 8))
                with m.If(event_count == 3):
                    m.next = "CRC-Low"
                with m.Else():
                    m.d.sd += event_count.eq(event_count + 1)

        with m.State("CRC-Low"):
            send_event_byte(event_crc[:8])
            with m.If(event_fire):
                m.next = "CRC-High"

        with m.State("CRC-High"):
            send_event_byte(event_crc[8:])
            with m.If(event_fire):
                m.next = "End"

        with m.State("End"):
            m.d.comb += [
                sd_to_host.i.valid.eq(1),
                sd_to_host.i.p.end.eq(1),
            ]
            with m.If(event_fire):
                m.d.sd += event_pending.eq(0)
                with m.If(event_kind == HostEvent.Read):
                    m.d.sd += read_in_flight.eq(1)
                m.next = "Idle"
