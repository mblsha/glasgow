from amaranth import *

from ._gateware_support import BLOCK_SIZE
from ._protocol import BackendStatus, HostEvent, HostReply


def elaborate_host_block_engine(
        m, component, host_to_sd, sd_to_host, block_write, block_read, core):
    self = component
    current_address = core.current_address
    erase_start = core.erase_start
    erase_end = core.erase_end
    read_request = core.read_request
    write_request = core.write_request
    erase_request = core.erase_request
    # Host replies carry both a session generation and a request ID. This permits the SD
    # clock domains and USB pipes to be reset independently without ever accepting a reply
    # left over from the previous target-power cycle.
    read_buffer_ready = Signal()
    read_buffer_address = Signal(32)
    read_buffer_consume = Signal()
    read_in_flight = Signal()
    read_in_flight_address = Signal(32)
    read_in_flight_request = Signal(16)
    read_reply_error = Signal()
    host_ack_valid = Signal()
    host_ack_status = Signal(8)
    host_ack_consume = Signal()
    ack_in_flight = Signal()
    ack_in_flight_opcode = Signal(8)
    ack_in_flight_request = Signal(16)

    reply_opcode = Signal(8)
    reply_generation = Signal(16)
    reply_request = Signal(16)
    reply_status = Signal(8)
    reply_header_count = Signal(range(2))
    reply_count = Signal(range(BLOCK_SIZE + 1))
    reply_valid = Signal()

    m.d.sd += read_reply_error.eq(0)
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
    with m.FSM(domain="sd", name="host_reply"):
        with m.State("Opcode"):
            m.d.comb += host_to_sd.o.ready.eq(1)
            with m.If(host_to_sd.o.valid):
                m.d.sd += [
                    reply_opcode.eq(host_to_sd.o.payload),
                    reply_header_count.eq(0),
                ]
                m.next = "Generation"

        with m.State("Generation"):
            m.d.comb += host_to_sd.o.ready.eq(1)
            with m.If(host_to_sd.o.valid):
                m.d.sd += reply_generation.word_select(reply_header_count, 8).eq(
                    host_to_sd.o.payload)
                with m.If(reply_header_count == 1):
                    m.d.sd += reply_header_count.eq(0)
                    m.next = "Request"
                with m.Else():
                    m.d.sd += reply_header_count.eq(reply_header_count + 1)

        with m.State("Request"):
            m.d.comb += host_to_sd.o.ready.eq(1)
            with m.If(host_to_sd.o.valid):
                m.d.sd += reply_request.word_select(reply_header_count, 8).eq(
                    host_to_sd.o.payload)
                with m.If(reply_header_count == 1):
                    m.next = "Status"
                with m.Else():
                    m.d.sd += reply_header_count.eq(reply_header_count + 1)

        with m.State("Status"):
            m.d.comb += host_to_sd.o.ready.eq(1)
            with m.If(host_to_sd.o.valid):
                m.d.sd += reply_status.eq(host_to_sd.o.payload)
                with m.If(reply_opcode == HostReply.ReadData):
                    m.d.sd += [
                        reply_count.eq(0),
                        reply_valid.eq(read_in_flight &
                            (reply_generation == self.generation) &
                            (reply_request == read_in_flight_request)),
                    ]
                    m.next = "Read-Data"
                with m.Elif((reply_opcode == HostReply.WriteAck) |
                            (reply_opcode == HostReply.EraseAck)):
                    reply_matches = ack_in_flight & \
                        (reply_opcode == ack_in_flight_opcode) & \
                        (reply_generation == self.generation) & \
                        (reply_request == ack_in_flight_request)
                    with m.If(reply_matches):
                        m.d.sd += [
                            host_ack_status.eq(host_to_sd.o.payload),
                            host_ack_valid.eq(1),
                            ack_in_flight.eq(0),
                        ]
                        with m.If(host_to_sd.o.payload != BackendStatus.OK):
                            m.d.sd += self.backend_errors.eq(self.backend_errors + 1)
                    with m.Else():
                        m.d.sd += self.stale_replies.eq(self.stale_replies + 1)
                    m.next = "Opcode"
                with m.Else():
                    m.d.sd += self.protocol_errors.eq(self.protocol_errors + 1)
                    m.next = "Opcode"

        with m.State("Read-Data"):
            m.d.comb += [
                host_to_sd.o.ready.eq(1),
                block_write.en.eq(host_to_sd.o.valid & reply_valid &
                                  (reply_status == BackendStatus.OK)),
                block_write.addr.eq(reply_count),
                block_write.data.eq(host_to_sd.o.payload),
            ]
            with m.If(host_to_sd.o.valid):
                with m.If(reply_count == BLOCK_SIZE - 1):
                    with m.If(reply_valid):
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
                    m.next = "Opcode"
                with m.Else():
                    m.d.sd += reply_count.eq(reply_count + 1)

    # Events use the corresponding request header. The `end` bit flushes each complete event
    # promptly rather than waiting for a full USB endpoint packet.
    event_address = Signal(32)
    event_request = Signal(16)
    next_request = Signal(16, init=1)
    event_count = Signal(range(BLOCK_SIZE + 1))
    event_read_address = Signal(range(BLOCK_SIZE))
    event_read_advance = Signal()
    event_kind = Signal(8, init=HostEvent.Read)
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
            read_request.eq(0),
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
            write_request.eq(0),
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
            erase_request.eq(0),
        ]

    m.d.comb += [
        sd_to_host.i.valid.eq(0),
        sd_to_host.i.p.data.eq(0),
        sd_to_host.i.p.end.eq(0),
        event_read_advance.eq(0),
    ]

    with m.FSM(domain="sd", name="host_event"):
        with m.State("Idle"):
            with m.If(event_pending):
                m.d.sd += event_count.eq(0)
                m.next = "Opcode"

        with m.State("Opcode"):
            m.d.comb += [
                sd_to_host.i.valid.eq(1),
                sd_to_host.i.p.data.eq(event_kind),
            ]
            with m.If(sd_to_host.i.ready):
                m.next = "Generation"

        with m.State("Generation"):
            m.d.comb += [
                sd_to_host.i.valid.eq(1),
                sd_to_host.i.p.data.eq(self.generation.word_select(event_count, 8)),
            ]
            with m.If(sd_to_host.i.ready):
                with m.If(event_count == 1):
                    m.d.sd += event_count.eq(0)
                    m.next = "Request"
                with m.Else():
                    m.d.sd += event_count.eq(event_count + 1)

        with m.State("Request"):
            m.d.comb += [
                sd_to_host.i.valid.eq(1),
                sd_to_host.i.p.data.eq(event_request.word_select(event_count, 8)),
            ]
            with m.If(sd_to_host.i.ready):
                with m.If(event_count == 1):
                    m.d.sd += event_count.eq(0)
                    m.next = "Address"
                with m.Else():
                    m.d.sd += event_count.eq(event_count + 1)

        with m.State("Address"):
            m.d.comb += [
                sd_to_host.i.valid.eq(1),
                sd_to_host.i.p.data.eq(event_address.word_select(event_count, 8)),
            ]
            with m.If(sd_to_host.i.ready):
                with m.If(event_count == 3):
                    m.d.sd += event_count.eq(0)
                    with m.Switch(event_kind):
                        with m.Case(HostEvent.Read):
                            m.d.comb += sd_to_host.i.p.end.eq(1)
                            m.d.sd += [
                                event_pending.eq(0),
                                read_in_flight.eq(1),
                            ]
                            m.next = "Idle"
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
            m.d.comb += [
                sd_to_host.i.valid.eq(1),
                sd_to_host.i.p.data.eq(block_read.data),
            ]
            with m.If(sd_to_host.i.ready):
                with m.If(event_count == BLOCK_SIZE - 1):
                    m.d.comb += sd_to_host.i.p.end.eq(1)
                    m.d.sd += event_pending.eq(0)
                    m.next = "Idle"
                with m.Else():
                    m.d.comb += event_read_advance.eq(1)
                    m.d.sd += [
                        event_count.eq(event_count + 1),
                        event_read_address.eq(event_count + 1),
                    ]

        with m.State("Erase-End"):
            m.d.comb += [
                sd_to_host.i.valid.eq(1),
                sd_to_host.i.p.data.eq(erase_end.word_select(event_count, 8)),
            ]
            with m.If(sd_to_host.i.ready):
                with m.If(event_count == 3):
                    m.d.comb += sd_to_host.i.p.end.eq(1)
                    m.d.sd += event_pending.eq(0)
                    m.next = "Idle"
                with m.Else():
                    m.d.sd += event_count.eq(event_count + 1)


    core.read_buffer_ready = read_buffer_ready
    core.read_buffer_address = read_buffer_address
    core.read_buffer_consume = read_buffer_consume
    core.read_reply_error = read_reply_error
    core.host_ack_valid = host_ack_valid
    core.host_ack_status = host_ack_status
    core.host_ack_consume = host_ack_consume
    core.event_pending = event_pending
    core.event_kind = event_kind
    core.event_count = event_count
    core.event_read_address = event_read_address
    core.event_read_advance = event_read_advance
