from amaranth import *
from amaranth.lib import memory, wiring
from amaranth.lib.wiring import In, Out

from ._gateware_support import BLOCK_SIZE
from ._interfaces import ReadBufferState


__all__ = ["BUFFER_BLOCKS", "BUFFER_SIZE", "BlockBuffer", "BlockBufferPortSignature"]


BUFFER_BLOCKS = 2
BUFFER_SIZE = BLOCK_SIZE * BUFFER_BLOCKS
BUFFER_BANK_BIT = BLOCK_SIZE.bit_length() - 1


class BlockBufferPortSignature(wiring.Signature):
    """A block-buffer port, oriented from an engine toward the buffer."""

    def __init__(self):
        super().__init__({
            "write_en": wiring.Out(1),
            "write_address": wiring.Out(range(BUFFER_SIZE)),
            "write_data": wiring.Out(8),
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
            "bank_states": In(6),
            "collision": Out(1),
            "collision_count": Out(16),
            "ownership_violation": Out(1),
            "fault_count": Out(16),
            "fault": Out(1),
        })

    def elaborate(self, platform):
        m = Module()

        m.submodules.memory = block_memory = memory.Memory(shape=8, depth=BUFFER_SIZE, init=[])
        write_port = block_memory.write_port(domain="sd")
        read_port = block_memory.read_port(domain="sd")

        fault_active = Signal()
        host_write_state = self.bank_states.word_select(
            self.host.write_address[BUFFER_BANK_BIT], 3)
        data_read_state = self.bank_states.word_select(
            self.data.read_address[BUFFER_BANK_BIT], 3)
        m.d.comb += self.collision.eq(
            (self.host.write_en & self.data.write_en) |
            (self.host.read_active & self.data.read_active) |
            (self.host.write_en & self.data.read_active &
                (self.host.write_address[BUFFER_BANK_BIT] ==
                 self.data.read_address[BUFFER_BANK_BIT])) |
            (self.data.write_en & self.host.read_active &
                (self.data.write_address[BUFFER_BANK_BIT] ==
                 self.host.read_address[BUFFER_BANK_BIT])))
        m.d.comb += self.ownership_violation.eq(
            (self.host.write_en &
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
        host_write_grant = healthy & self.host.write_en
        data_write_grant = healthy & self.data.write_en
        host_read_grant = healthy & self.host.read_active
        data_read_grant = healthy & ~self.host.read_active & self.data.read_active

        m.d.sd += [
            Assert(~(host_write_grant & data_write_grant)),
            Assert(~(host_read_grant & data_read_grant)),
        ]

        m.d.comb += [
            write_port.en.eq(host_write_grant | data_write_grant),
            write_port.addr.eq(Mux(
                host_write_grant, self.host.write_address, self.data.write_address)),
            write_port.data.eq(Mux(
                host_write_grant, self.host.write_data, self.data.write_data)),
            read_port.addr.eq(Mux(
                host_read_grant, self.host.read_address,
                Mux(data_read_grant, self.data.read_address, 0))),
            self.host.read_data.eq(read_port.data),
            self.data.read_data.eq(read_port.data),
        ]

        return m
