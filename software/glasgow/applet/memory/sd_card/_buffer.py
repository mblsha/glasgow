from amaranth import *
from amaranth.lib import memory, wiring
from amaranth.lib.wiring import In, Out

from ._gateware_support import BLOCK_SIZE


__all__ = ["BlockBuffer", "BlockBufferPortSignature"]


class BlockBufferPortSignature(wiring.Signature):
    """A block-buffer port, oriented from an engine toward the buffer."""

    def __init__(self):
        super().__init__({
            "write_en": wiring.Out(1),
            "write_address": wiring.Out(range(BLOCK_SIZE)),
            "write_data": wiring.Out(8),
            "read_active": wiring.Out(1),
            "read_address": wiring.Out(range(BLOCK_SIZE)),
            "read_data": wiring.In(8),
        })


class BlockBuffer(wiring.Component):
    """One-block staging RAM with explicit host and SD-data-engine clients.

    The host client owns the read port while ``read_active`` is asserted; otherwise the data
    engine owns it. Host and data writes are mutually exclusive at the protocol level, with host
    priority providing deterministic behavior if a malformed exchange violates that invariant.
    """

    def __init__(self):
        super().__init__({
            "host": In(BlockBufferPortSignature()),
            "data": In(BlockBufferPortSignature()),
            "collision": Out(1),
            "collision_count": Out(16),
        })

    def elaborate(self, platform):
        m = Module()

        m.submodules.memory = block_memory = memory.Memory(shape=8, depth=BLOCK_SIZE, init=[])
        write_port = block_memory.write_port(domain="sd")
        read_port = block_memory.read_port(domain="sd")

        host_write_grant = self.host.write_en
        data_write_grant = ~self.host.write_en & self.data.write_en
        host_read_grant = self.host.read_active
        data_read_grant = ~self.host.read_active & self.data.read_active

        collision_active = Signal()
        m.d.comb += self.collision.eq(
            (self.host.write_en & self.data.write_en) |
            (self.host.read_active & self.data.read_active))
        m.d.sd += collision_active.eq(self.collision)
        with m.If(self.collision & ~collision_active):
            m.d.sd += self.collision_count.eq(self.collision_count + 1)

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
