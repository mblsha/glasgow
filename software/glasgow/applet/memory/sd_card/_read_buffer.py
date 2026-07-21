from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.wiring import In, Out

from ._interfaces import ReadBufferState, ReadHostSignature


__all__ = ["ReadBufferClientSignature", "ReadBufferManager"]


class ReadBufferClientSignature(wiring.Signature):
    """Read-buffer controls, oriented from the data engine toward the manager."""

    def __init__(self):
        super().__init__({
            "reset": Out(1),
            "lookup_address": Out(32),
            "reserve": Out(1),
            "reserve_address": Out(32),
            "reserve_ready": In(1),
            "ready": In(1),
            "filling": In(1),
            "activate": Out(1),
            "release": Out(1),
            "active": In(1),
            "active_bank": In(1),
            "bank_states": In(6),
            "reply_fault": In(1),
        })


class ReadBufferManager(wiring.Component):
    """Own the tagged lifecycle of both host-filled read banks."""

    client: In(ReadBufferClientSignature())
    host: Out(ReadHostSignature())

    def elaborate(self, platform):
        m = Module()

        client = self.client
        host = self.host
        bank_state = Array(
            Signal(ReadBufferState, name=f"bank_{index}_state") for index in range(2))
        bank_address = Array(Signal(32, name=f"bank_{index}_address") for index in range(2))
        next_state = Array(
            Signal(ReadBufferState, name=f"bank_{index}_next_state") for index in range(2))
        next_address = Array(
            Signal(32, name=f"bank_{index}_next_address") for index in range(2))

        request_next = Signal()
        request_address_next = Signal(32)
        request_bank_next = Signal()
        reply_fault_next = Signal()
        empty_bank = Signal()
        empty_available = Signal()
        reserve_address_present = Signal()
        ready_bank = Signal()
        active_count = Signal(range(3))

        m.d.comb += [
            request_next.eq(host.request),
            request_address_next.eq(host.request_address),
            request_bank_next.eq(host.request_bank),
            reply_fault_next.eq(0),
            empty_bank.eq(Mux(bank_state[0] == ReadBufferState.Empty, 0, 1)),
            empty_available.eq(
                (bank_state[0] == ReadBufferState.Empty) |
                (bank_state[1] == ReadBufferState.Empty)),
            reserve_address_present.eq(
                ((bank_state[0] != ReadBufferState.Empty) &
                 (bank_address[0] == client.reserve_address)) |
                ((bank_state[1] != ReadBufferState.Empty) &
                 (bank_address[1] == client.reserve_address))),
            ready_bank.eq(Mux(
                (bank_state[0] == ReadBufferState.Ready) &
                (bank_address[0] == client.lookup_address), 0, 1)),
            client.ready.eq(
                ((bank_state[0] == ReadBufferState.Ready) &
                 (bank_address[0] == client.lookup_address)) |
                ((bank_state[1] == ReadBufferState.Ready) &
                 (bank_address[1] == client.lookup_address))),
            client.filling.eq(
                ((bank_state[0] == ReadBufferState.Filling) &
                 (bank_address[0] == client.lookup_address)) |
                ((bank_state[1] == ReadBufferState.Filling) &
                 (bank_address[1] == client.lookup_address))),
            client.active.eq(
                (bank_state[0] == ReadBufferState.Active) |
                (bank_state[1] == ReadBufferState.Active)),
            client.active_bank.eq(bank_state[1] == ReadBufferState.Active),
            client.bank_states.eq(Cat(bank_state[0], bank_state[1])),
            client.reserve_ready.eq(
                ~host.request & empty_available & ~reserve_address_present),
            host.reply_consume.eq(host.reply_ready),
            active_count.eq(
                (bank_state[0] == ReadBufferState.Active) +
                (bank_state[1] == ReadBufferState.Active)),
        ]
        for bank in range(2):
            m.d.comb += [
                next_state[bank].eq(bank_state[bank]),
                next_address[bank].eq(bank_address[bank]),
            ]

        # The following is the complete transition priority, from ordinary events to the
        # highest-priority reset/cancel path. Each state register is assigned exactly once below.
        with m.If(host.request_accepted):
            m.d.comb += request_next.eq(0)

        with m.If(client.reserve & client.reserve_ready):
            m.d.comb += [
                request_next.eq(1),
                request_address_next.eq(client.reserve_address),
                request_bank_next.eq(empty_bank),
                next_state[empty_bank].eq(ReadBufferState.Filling),
                next_address[empty_bank].eq(client.reserve_address),
            ]

        with m.If(host.reply_ready):
            reply_state = bank_state[host.reply_bank]
            reply_address_matches = bank_address[host.reply_bank] == host.reply_address
            with m.Switch(reply_state):
                with m.Case(ReadBufferState.Filling):
                    with m.If(reply_address_matches):
                        m.d.comb += next_state[host.reply_bank].eq(ReadBufferState.Ready)
                    with m.Else():
                        m.d.comb += [
                            next_state[host.reply_bank].eq(ReadBufferState.Empty),
                            reply_fault_next.eq(1),
                        ]
                with m.Case(ReadBufferState.Draining):
                    m.d.comb += next_state[host.reply_bank].eq(ReadBufferState.Empty)
                    with m.If(~reply_address_matches):
                        m.d.comb += reply_fault_next.eq(1)
                with m.Default():
                    m.d.comb += reply_fault_next.eq(1)

        with m.If(host.reply_error):
            with m.Switch(bank_state[host.reply_bank]):
                with m.Case(ReadBufferState.Filling):
                    m.d.comb += [
                        next_state[host.reply_bank].eq(ReadBufferState.Empty),
                        reply_fault_next.eq(1),
                    ]
                with m.Case(ReadBufferState.Draining):
                    m.d.comb += next_state[host.reply_bank].eq(ReadBufferState.Empty)
                with m.Default():
                    m.d.comb += reply_fault_next.eq(1)

        with m.If(client.activate):
            with m.If(client.ready):
                m.d.comb += next_state[ready_bank].eq(ReadBufferState.Active)
            with m.Else():
                m.d.comb += reply_fault_next.eq(1)

        with m.If(client.release):
            with m.If(client.active):
                m.d.comb += next_state[client.active_bank].eq(ReadBufferState.Empty)
            with m.Else():
                m.d.comb += reply_fault_next.eq(1)

        with m.If(client.reset):
            m.d.comb += [request_next.eq(0), reply_fault_next.eq(0)]
            for bank, state in enumerate(bank_state):
                completion = (host.reply_ready | host.reply_error) & (host.reply_bank == bank)
                with m.If(completion):
                    # A completion consumed in the reset cycle must not be left Draining.
                    m.d.comb += next_state[bank].eq(ReadBufferState.Empty)
                with m.Elif(state == ReadBufferState.Filling):
                    # Once accepted, a canceled request retains host write ownership until its
                    # payload drains. Requests that have not crossed the boundary can be dropped.
                    with m.If(host.request & (host.request_bank == bank) &
                              ~host.request_accepted):
                        m.d.comb += next_state[bank].eq(ReadBufferState.Empty)
                    with m.Else():
                        m.d.comb += next_state[bank].eq(ReadBufferState.Draining)
                with m.Elif(state == ReadBufferState.Draining):
                    m.d.comb += next_state[bank].eq(ReadBufferState.Draining)
                with m.Else():
                    m.d.comb += next_state[bank].eq(ReadBufferState.Empty)

        m.d.sd += [
            host.request.eq(request_next),
            host.request_address.eq(request_address_next),
            host.request_bank.eq(request_bank_next),
            client.reply_fault.eq(reply_fault_next),
            Assert(active_count <= 1),
        ]
        for bank in range(2):
            m.d.sd += [
                bank_state[bank].eq(next_state[bank]),
                bank_address[bank].eq(next_address[bank]),
            ]
        with m.If(host.request):
            m.d.sd += [
                Assert(bank_state[host.request_bank] == ReadBufferState.Filling),
                Assert(bank_address[host.request_bank] == host.request_address),
            ]

        return m
