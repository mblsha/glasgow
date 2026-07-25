from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.wiring import In, Out

from ._interfaces import ReadBufferState, ReadHostSignature
from ._gateware_support import (
    BLOCK_SIZE, BUFFER_BANK_BITS, BUFFER_BLOCKS, BUFFER_STATE_WIDTH,
)


__all__ = ["ReadBufferClientSignature", "ReadBufferManager"]


class ReadBufferClientSignature(wiring.Signature):
    """Read-buffer controls, oriented from the data engine toward the manager."""

    def __init__(self):
        super().__init__({
            "reset": Out(1),
            "preload_boot": Out(1),
            "preload_boot_address_0": Out(32),
            "preload_boot_address_1": Out(32),
            "lookup_address": Out(32),
            "reserve": Out(1),
            "reserve_address": Out(32),
            "reserve_blocks": Out(2),
            "reserve_ready": In(1),
            "ready": In(1),
            "next_ready": In(1),
            "filling": In(1),
            "discard_stale": Out(1),
            "activate": Out(1),
            "release": Out(1),
            "active": In(1),
            "active_bank": In(BUFFER_BANK_BITS),
            "bank_states": In(BUFFER_STATE_WIDTH),
            "ready_ahead": In(range(BUFFER_BLOCKS + 1)),
            "reply_fault": In(1),
        })


class ReadBufferManager(wiring.Component):
    """Own the tagged lifecycle of the host-filled read reservoir."""

    client: In(ReadBufferClientSignature())
    host: Out(ReadHostSignature())

    def elaborate(self, platform):
        m = Module()

        client = self.client
        host = self.host
        bank_state = Array(
            Signal(ReadBufferState, name=f"bank_{index}_state")
            for index in range(BUFFER_BLOCKS))
        bank_address = Array(
            Signal(32, name=f"bank_{index}_address")
            for index in range(BUFFER_BLOCKS))
        bank_pinned = Array(
            Signal(name=f"bank_{index}_pinned")
            for index in range(BUFFER_BLOCKS))
        next_state = Array(
            Signal(ReadBufferState, name=f"bank_{index}_next_state")
            for index in range(BUFFER_BLOCKS))
        next_address = Array(
            Signal(32, name=f"bank_{index}_next_address")
            for index in range(BUFFER_BLOCKS))
        next_pinned = Array(
            Signal(name=f"bank_{index}_next_pinned")
            for index in range(BUFFER_BLOCKS))

        request_next = Signal()
        request_address_next = Signal(32)
        request_bank_next = Signal(BUFFER_BANK_BITS)
        request_blocks_next = Signal(2)
        reply_fault_next = Signal()
        empty_bank = Signal(BUFFER_BANK_BITS)
        empty_available = Signal()
        reserve_address_present = Signal()
        ready_bank = Signal(BUFFER_BANK_BITS)
        active_count = Signal(range(BUFFER_BLOCKS + 1))
        ready_prefix = Array(
            Signal(name=f"ready_prefix_{index}") for index in range(BUFFER_BLOCKS))

        m.d.comb += [
            request_next.eq(host.request),
            request_address_next.eq(host.request_address),
            request_bank_next.eq(host.request_bank),
            request_blocks_next.eq(host.request_blocks),
            reply_fault_next.eq(0),
            empty_bank.eq(0),
            empty_available.eq(0),
            reserve_address_present.eq(0),
            ready_bank.eq(0),
            client.ready.eq(0),
            client.next_ready.eq(0),
            client.filling.eq(0),
            client.active.eq(0),
            client.active_bank.eq(0),
            client.bank_states.eq(Cat(*bank_state)),
            client.ready_ahead.eq(sum(ready_prefix)),
            client.reserve_ready.eq(
                ~host.request & empty_available & ~reserve_address_present),
            host.reply_consume.eq(host.reply_ready),
            active_count.eq(sum(
                state == ReadBufferState.Active for state in bank_state)),
        ]
        for bank in reversed(range(BUFFER_BLOCKS)):
            with m.If(bank_state[bank] == ReadBufferState.Empty):
                m.d.comb += [empty_available.eq(1), empty_bank.eq(bank)]
            with m.If((bank_state[bank] != ReadBufferState.Empty) &
                      (bank_address[bank] == client.reserve_address)):
                m.d.comb += reserve_address_present.eq(1)
            with m.If((bank_state[bank] == ReadBufferState.Ready) &
                      (bank_address[bank] == client.lookup_address)):
                m.d.comb += [client.ready.eq(1), ready_bank.eq(bank)]
            with m.If((bank_state[bank] == ReadBufferState.Ready) &
                      (bank_address[bank] == client.lookup_address + BLOCK_SIZE)):
                m.d.comb += client.next_ready.eq(1)
            with m.If((bank_state[bank] == ReadBufferState.Filling) &
                      (bank_address[bank] == client.lookup_address)):
                m.d.comb += client.filling.eq(1)
            with m.If(bank_state[bank] == ReadBufferState.Active):
                m.d.comb += [client.active.eq(1), client.active_bank.eq(bank)]

        for offset in range(BUFFER_BLOCKS):
            ready_at_offset = Cat(*[
                (bank_state[bank] == ReadBufferState.Ready) &
                (bank_address[bank] == client.lookup_address + offset * BLOCK_SIZE)
                for bank in range(BUFFER_BLOCKS)]).any()
            if offset == 0:
                m.d.comb += ready_prefix[offset].eq(ready_at_offset)
            else:
                m.d.comb += ready_prefix[offset].eq(
                    ready_prefix[offset - 1] & ready_at_offset)

        for bank in range(BUFFER_BLOCKS):
            m.d.comb += [
                next_state[bank].eq(bank_state[bank]),
                next_address[bank].eq(bank_address[bank]),
                next_pinned[bank].eq(bank_pinned[bank]),
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
                request_blocks_next.eq(client.reserve_blocks),
                next_state[empty_bank].eq(ReadBufferState.Filling),
                next_address[empty_bank].eq(client.reserve_address),
                next_pinned[empty_bank].eq(0),
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
                    with m.If(reply_address_matches):
                        m.d.comb += next_state[host.reply_bank].eq(ReadBufferState.Ready)
                    with m.Else():
                        m.d.comb += next_state[host.reply_bank].eq(ReadBufferState.Empty)
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
                m.d.comb += [
                    next_state[client.active_bank].eq(ReadBufferState.Empty),
                    next_pinned[client.active_bank].eq(0),
                ]
            with m.Else():
                m.d.comb += reply_fault_next.eq(1)

        with m.If(client.discard_stale):
            for bank, state in enumerate(bank_state):
                in_window = (bank_address[bank] == client.lookup_address) | \
                    (bank_address[bank] == client.lookup_address + BLOCK_SIZE)
                # Preserve the non-contiguous companion boot block only while the requested
                # address itself hits the embedded set. An unrelated first access must be able
                # to evict both pinned banks and use the ordinary host-backed reservoir.
                with m.If((state == ReadBufferState.Ready) & ~in_window &
                          (~bank_pinned[bank] | ~client.ready)):
                    m.d.comb += next_state[bank].eq(ReadBufferState.Empty)

        with m.If(client.reset):
            m.d.comb += [request_next.eq(0), reply_fault_next.eq(0)]
            for bank, state in enumerate(bank_state):
                completion = (host.reply_ready | host.reply_error) & (host.reply_bank == bank)
                with m.If(completion):
                    # A validated read that completes after CMD12 is useful as the first-data
                    # cache for the next sequential command.
                    with m.If(host.reply_ready &
                              (host.reply_address == bank_address[bank])):
                        m.d.comb += next_state[bank].eq(ReadBufferState.Ready)
                    with m.Else():
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
                with m.Elif(state == ReadBufferState.Ready):
                    m.d.comb += next_state[bank].eq(ReadBufferState.Ready)
                with m.Else():
                    m.d.comb += [
                        next_state[bank].eq(ReadBufferState.Empty),
                        next_pinned[bank].eq(0),
                    ]

        with m.If(client.preload_boot):
            m.d.comb += [request_next.eq(0), reply_fault_next.eq(0)]
            for bank in range(BUFFER_BLOCKS):
                m.d.comb += [
                    next_state[bank].eq(ReadBufferState.Ready),
                    next_address[bank].eq(
                        client.preload_boot_address_0 if bank == 0
                        else client.preload_boot_address_1),
                    next_pinned[bank].eq(1),
                ]

        m.d.sd += [
            host.request.eq(request_next),
            host.request_address.eq(request_address_next),
            host.request_bank.eq(request_bank_next),
            host.request_blocks.eq(request_blocks_next),
            client.reply_fault.eq(reply_fault_next),
            Assert(active_count <= 1),
        ]
        for bank in range(BUFFER_BLOCKS):
            m.d.sd += [
                bank_state[bank].eq(next_state[bank]),
                bank_address[bank].eq(next_address[bank]),
                bank_pinned[bank].eq(next_pinned[bank]),
            ]
        with m.If(host.request):
            m.d.sd += [
                Assert(bank_state[host.request_bank] == ReadBufferState.Filling),
                Assert(bank_address[host.request_bank] == host.request_address),
            ]

        return m
