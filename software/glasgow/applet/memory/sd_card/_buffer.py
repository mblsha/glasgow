from amaranth import *
from amaranth.lib import memory, wiring
from amaranth.lib.wiring import In, Out

from ._gateware_support import (
    BLOCK_SIZE, BUFFER_BANK_BITS, BUFFER_BLOCKS, BUFFER_STATE_WIDTH,
)
from ._interfaces import ReadBufferState


__all__ = ["BUFFER_BLOCKS", "BUFFER_SIZE", "BlockBuffer", "BlockBufferPortSignature"]


BUFFER_SIZE = BLOCK_SIZE * BUFFER_BLOCKS
BUFFER_BANK_BIT = BLOCK_SIZE.bit_length() - 1


class BlockBufferPortSignature(wiring.Signature):
    """A block-buffer port, oriented from an engine toward the buffer."""

    def __init__(self):
        super().__init__({
            "write_en": wiring.Out(8),
            "write_address": wiring.Out(range(BUFFER_SIZE)),
            "write_data": wiring.Out(64),
            "read_active": wiring.Out(1),
            "read_address": wiring.Out(range(BUFFER_SIZE)),
            "read_data": wiring.In(8),
        })


class BlockBuffer(wiring.Component):
    """Two-bank staging RAM with explicit host and SD-data-engine clients.

    The host client owns the read port while ``read_active`` is asserted; otherwise the data
    engine owns it. Protocol-level ownership violations fail closed and poison the buffer until
    the SD domain is reset rather than selecting a client and silently corrupting a block.
    """

    def __init__(self):
        super().__init__({
            "host": In(BlockBufferPortSignature()),
            "data": In(BlockBufferPortSignature()),
            "bank_states": In(BUFFER_STATE_WIDTH),
            "collision": Out(1),
            "collision_count": Out(16),
            "ownership_violation": Out(1),
            "fault_count": Out(16),
            "fault": Out(1),
        })

    def elaborate(self, platform):
        m = Module()

        m.submodules.memory = block_memory = memory.Memory(
            shape=64, depth=BUFFER_SIZE // 8, init=[])
        write_port = block_memory.write_port(domain="sd", granularity=8)
        read_port = block_memory.read_port(domain="sd")

        fault_active = Signal()
        host_write_state = self.bank_states.word_select(
            self.host.write_address[BUFFER_BANK_BIT:BUFFER_BANK_BIT + BUFFER_BANK_BITS], 3)
        data_read_state = self.bank_states.word_select(
            self.data.read_address[BUFFER_BANK_BIT:BUFFER_BANK_BIT + BUFFER_BANK_BITS], 3)
        m.d.comb += self.collision.eq(
            (self.host.write_en.any() & self.data.write_en.any()) |
            (self.host.read_active & self.data.read_active) |
            (self.host.write_en.any() & self.data.read_active &
                (self.host.write_address[BUFFER_BANK_BIT:BUFFER_BANK_BIT + BUFFER_BANK_BITS] ==
                 self.data.read_address[BUFFER_BANK_BIT:BUFFER_BANK_BIT + BUFFER_BANK_BITS])) |
            (self.data.write_en.any() & self.host.read_active &
                (self.data.write_address[BUFFER_BANK_BIT:BUFFER_BANK_BIT + BUFFER_BANK_BITS] ==
                 self.host.read_address[BUFFER_BANK_BIT:BUFFER_BANK_BIT + BUFFER_BANK_BITS])))
        m.d.comb += self.ownership_violation.eq(
            (self.host.write_en.any() &
             (host_write_state != ReadBufferState.Filling) &
             (host_write_state != ReadBufferState.Draining)) |
            (self.data.read_active & (data_read_state != ReadBufferState.Active)))
        fault_event = self.collision | self.ownership_violation
        m.d.sd += fault_active.eq(fault_event)
        with m.If(self.collision & ~fault_active):
            m.d.sd += self.collision_count.eq(self.collision_count + 1)
        with m.If(fault_event & ~fault_active):
            m.d.sd += self.fault_count.eq(self.fault_count + 1)
        with m.If(fault_event):
            m.d.sd += self.fault.eq(1)

        # Any ownership violation poisons the buffer until the SD domain is reset. Both clients
        # are denied on the collision cycle as well, so priority cannot silently corrupt a block.
        healthy = ~fault_event & ~self.fault
        host_write_grant = healthy & self.host.write_en.any()
        data_write_grant = healthy & self.data.write_en.any()
        host_read_grant = healthy & self.host.read_active
        data_read_grant = healthy & ~self.host.read_active & self.data.read_active
        read_byte_select = Signal(3)

        m.d.sd += [
            Assert(~(host_write_grant & data_write_grant)),
            Assert(~(host_read_grant & data_read_grant)),
            # Align the byte lane with the synchronous memory output. The clients present the
            # following byte address on the final bit of the current byte to hide BRAM latency;
            # a combinational lane mux would otherwise switch one cycle too early.
            read_byte_select.eq(Mux(
                host_read_grant, self.host.read_address[:3],
                Mux(data_read_grant, self.data.read_address[:3], 0))),
        ]

        m.d.comb += [
            write_port.en.eq(Mux(host_write_grant, self.host.write_en,
                Mux(data_write_grant, self.data.write_en, 0))),
            write_port.addr.eq(Mux(
                host_write_grant, self.host.write_address[3:],
                self.data.write_address[3:])),
            write_port.data.eq(Mux(
                host_write_grant, self.host.write_data, self.data.write_data)),
            read_port.addr.eq(Mux(
                host_read_grant, self.host.read_address[3:],
                Mux(data_read_grant, self.data.read_address[3:], 0))),
            self.host.read_data.eq(read_port.data.word_select(read_byte_select, 8)),
            self.data.read_data.eq(read_port.data.word_select(read_byte_select, 8)),
        ]

        return m
