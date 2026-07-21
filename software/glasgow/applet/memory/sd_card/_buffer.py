from amaranth import *
from amaranth.lib import memory, wiring
from amaranth.lib.wiring import In

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
        })

    def elaborate(self, platform):
        m = Module()

        m.submodules.memory = block_memory = memory.Memory(shape=8, depth=BLOCK_SIZE, init=[])
        write_port = block_memory.write_port(domain="sd")
        read_port = block_memory.read_port(domain="sd")

        m.d.comb += [
            write_port.en.eq(self.host.write_en | self.data.write_en),
            write_port.addr.eq(Mux(
                self.host.write_en, self.host.write_address, self.data.write_address)),
            write_port.data.eq(Mux(
                self.host.write_en, self.host.write_data, self.data.write_data)),
            read_port.addr.eq(Mux(
                self.host.read_active, self.host.read_address, self.data.read_address)),
            self.host.read_data.eq(read_port.data),
            self.data.read_data.eq(read_port.data),
        ]

        return m
