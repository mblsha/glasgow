from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.wiring import In, Out

from ._interfaces import ReadBufferState


__all__ = [
    "ReadBufferClientSignature", "ReadBufferHostSignature", "ReadBufferManager",
]


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


class ReadBufferHostSignature(wiring.Signature):
    """Read-fill request/reply channel, oriented from the manager toward the host engine."""

    def __init__(self):
        super().__init__({
            "request": Out(1),
            "request_address": Out(32),
            "request_bank": Out(1),
            "request_accepted": In(1),
            "reply_ready": In(1),
            "reply_address": In(32),
            "reply_bank": In(1),
            "reply_error": In(1),
            "reply_consume": Out(1),
        })


class ReadBufferManager(wiring.Component):
    """Own the tagged lifecycle of both host-filled read banks."""

    client: In(ReadBufferClientSignature())
    host: Out(ReadBufferHostSignature())

    def elaborate(self, platform):
        m = Module()

        client = self.client
        host = self.host
        bank_state = Array(
            Signal(ReadBufferState, name=f"bank_{index}_state") for index in range(2))
        bank_address = Array(Signal(32, name=f"bank_{index}_address") for index in range(2))

        empty_bank = Signal()
        empty_available = Signal()
        reserve_address_present = Signal()
        ready_bank = Signal()
        active_count = Signal(range(3))

        m.d.comb += [
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
        m.d.sd += [
            Assert(active_count <= 1),
            client.reply_fault.eq(0),
        ]
        with m.If(host.request):
            m.d.sd += [
                Assert(bank_state[host.request_bank] == ReadBufferState.Filling),
                Assert(bank_address[host.request_bank] == host.request_address),
            ]

        with m.If(host.request_accepted):
            m.d.sd += host.request.eq(0)

        with m.If(client.reserve & client.reserve_ready):
            m.d.sd += [
                host.request.eq(1),
                host.request_address.eq(client.reserve_address),
                host.request_bank.eq(empty_bank),
                bank_state[empty_bank].eq(ReadBufferState.Filling),
                bank_address[empty_bank].eq(client.reserve_address),
            ]

        with m.If(host.reply_ready):
            reply_state = bank_state[host.reply_bank]
            reply_address_matches = bank_address[host.reply_bank] == host.reply_address
            with m.Switch(reply_state):
                with m.Case(ReadBufferState.Filling):
                    with m.If(reply_address_matches):
                        m.d.sd += reply_state.eq(ReadBufferState.Ready)
                    with m.Else():
                        m.d.sd += [
                            reply_state.eq(ReadBufferState.Empty),
                            client.reply_fault.eq(1),
                        ]
                with m.Case(ReadBufferState.Draining):
                    m.d.sd += reply_state.eq(ReadBufferState.Empty)
                    with m.If(~reply_address_matches):
                        m.d.sd += client.reply_fault.eq(1)
                with m.Default():
                    m.d.sd += client.reply_fault.eq(1)

        with m.If(host.reply_error):
            reply_state = bank_state[host.reply_bank]
            with m.Switch(reply_state):
                with m.Case(ReadBufferState.Filling):
                    m.d.sd += [
                        reply_state.eq(ReadBufferState.Empty),
                        client.reply_fault.eq(1),
                    ]
                with m.Case(ReadBufferState.Draining):
                    m.d.sd += reply_state.eq(ReadBufferState.Empty)
                with m.Default():
                    m.d.sd += client.reply_fault.eq(1)

        with m.If(client.activate):
            with m.If(client.ready):
                m.d.sd += bank_state[ready_bank].eq(ReadBufferState.Active)
            with m.Else():
                m.d.sd += client.reply_fault.eq(1)

        with m.If(client.release):
            with m.If(client.active):
                m.d.sd += bank_state[client.active_bank].eq(ReadBufferState.Empty)
            with m.Else():
                m.d.sd += client.reply_fault.eq(1)

        with m.If(client.reset):
            m.d.sd += [
                host.request.eq(0),
                client.reply_fault.eq(0),
            ]
            for bank, state in enumerate(bank_state):
                with m.If(state == ReadBufferState.Filling):
                    # Once accepted, a canceled request retains host write ownership until its
                    # payload drains. Requests that have not crossed the boundary can be dropped.
                    with m.If((host.reply_ready | host.reply_error) &
                              (host.reply_bank == bank)):
                        # The completion logic above consumes the reply in this cycle. Reset must
                        # not overwrite Empty with Draining after the final payload has arrived.
                        m.d.sd += state.eq(ReadBufferState.Empty)
                    with m.Elif(host.request & (host.request_bank == bank) &
                              ~host.request_accepted):
                        m.d.sd += state.eq(ReadBufferState.Empty)
                    with m.Else():
                        m.d.sd += state.eq(ReadBufferState.Draining)
                with m.Elif(state != ReadBufferState.Draining):
                    m.d.sd += state.eq(ReadBufferState.Empty)

        return m
