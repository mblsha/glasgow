from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.wiring import In, Out

from ._interfaces import ReadBufferState


__all__ = ["ReadBufferManager"]


class ReadBufferManager(wiring.Component):
    """Own the tagged lifecycle of both host-filled read banks."""

    reset: In(1)
    lookup_address: In(32)

    reserve: In(1)
    reserve_address: In(32)
    reserve_ready: Out(1)

    ready: Out(1)
    filling: Out(1)
    activate: In(1)
    release: In(1)

    active: Out(1)
    active_bank: Out(1)
    bank_states: Out(4)
    reply_fault: Out(1)

    host_request: Out(1)
    host_request_address: Out(32)
    host_request_bank: Out(1)
    host_request_accepted: In(1)

    host_reply_ready: In(1)
    host_reply_address: In(32)
    host_reply_bank: In(1)
    host_reply_error: In(1)
    host_reply_consume: Out(1)

    def elaborate(self, platform):
        m = Module()

        bank_state = Array(
            Signal(ReadBufferState, name=f"bank_{index}_state") for index in range(2))
        bank_address = Array(Signal(32, name=f"bank_{index}_address") for index in range(2))
        bank_canceled = Array(Signal(name=f"bank_{index}_canceled") for index in range(2))

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
                 (bank_address[0] == self.reserve_address)) |
                ((bank_state[1] != ReadBufferState.Empty) &
                 (bank_address[1] == self.reserve_address))),
            ready_bank.eq(Mux(
                (bank_state[0] == ReadBufferState.Ready) &
                (bank_address[0] == self.lookup_address), 0, 1)),
            self.ready.eq(
                ((bank_state[0] == ReadBufferState.Ready) &
                 (bank_address[0] == self.lookup_address)) |
                ((bank_state[1] == ReadBufferState.Ready) &
                 (bank_address[1] == self.lookup_address))),
            self.filling.eq(
                ((bank_state[0] == ReadBufferState.Filling) &
                 (bank_address[0] == self.lookup_address)) |
                ((bank_state[1] == ReadBufferState.Filling) &
                 (bank_address[1] == self.lookup_address))),
            self.active.eq(
                (bank_state[0] == ReadBufferState.Active) |
                (bank_state[1] == ReadBufferState.Active)),
            self.active_bank.eq(bank_state[1] == ReadBufferState.Active),
            self.bank_states.eq(Cat(bank_state[0], bank_state[1])),
            self.reserve_ready.eq(
                ~self.host_request & empty_available & ~reserve_address_present),
            self.host_reply_consume.eq(self.host_reply_ready),
            active_count.eq(
                (bank_state[0] == ReadBufferState.Active) +
                (bank_state[1] == ReadBufferState.Active)),
        ]
        m.d.sd += [
            Assert(active_count <= 1),
            self.reply_fault.eq(0),
        ]

        with m.If(self.host_request_accepted):
            m.d.sd += self.host_request.eq(0)

        with m.If(self.reserve & self.reserve_ready):
            m.d.sd += [
                self.host_request.eq(1),
                self.host_request_address.eq(self.reserve_address),
                self.host_request_bank.eq(empty_bank),
                bank_state[empty_bank].eq(ReadBufferState.Filling),
                bank_address[empty_bank].eq(self.reserve_address),
                bank_canceled[empty_bank].eq(0),
            ]

        with m.If(self.host_reply_ready):
            with m.If((bank_state[self.host_reply_bank] == ReadBufferState.Filling) &
                      (bank_address[self.host_reply_bank] == self.host_reply_address)):
                m.d.sd += [
                    bank_state[self.host_reply_bank].eq(Mux(
                        bank_canceled[self.host_reply_bank],
                        ReadBufferState.Empty, ReadBufferState.Ready)),
                    bank_canceled[self.host_reply_bank].eq(0),
                ]
            with m.Elif(bank_state[self.host_reply_bank] == ReadBufferState.Filling):
                m.d.sd += [
                    bank_state[self.host_reply_bank].eq(ReadBufferState.Empty),
                    self.reply_fault.eq(1),
                ]

        with m.If(self.host_reply_error &
                  (bank_state[self.host_reply_bank] == ReadBufferState.Filling)):
            m.d.sd += [
                bank_state[self.host_reply_bank].eq(ReadBufferState.Empty),
                bank_canceled[self.host_reply_bank].eq(0),
            ]
            with m.If(~bank_canceled[self.host_reply_bank]):
                m.d.sd += self.reply_fault.eq(1)

        with m.If(self.activate):
            with m.If(self.ready):
                m.d.sd += bank_state[ready_bank].eq(ReadBufferState.Active)
            with m.Else():
                m.d.sd += self.reply_fault.eq(1)

        with m.If(self.release):
            with m.If(self.active):
                m.d.sd += bank_state[self.active_bank].eq(ReadBufferState.Empty)
            with m.Else():
                m.d.sd += self.reply_fault.eq(1)

        with m.If(self.reset):
            m.d.sd += [
                self.host_request.eq(0),
                self.reply_fault.eq(0),
            ]
            for bank, state in enumerate(bank_state):
                with m.If(state == ReadBufferState.Filling):
                    # An accepted transaction must keep ownership until its payload has drained.
                    # A request that has not crossed into the host engine can be canceled outright.
                    with m.If(self.host_request &
                              (self.host_request_bank == bank) &
                              ~self.host_request_accepted):
                        m.d.sd += [
                            state.eq(ReadBufferState.Empty),
                            bank_canceled[bank].eq(0),
                        ]
                    with m.Else():
                        m.d.sd += bank_canceled[bank].eq(1)
                with m.Else():
                    m.d.sd += [
                        state.eq(ReadBufferState.Empty),
                        bank_canceled[bank].eq(0),
                    ]

        return m
