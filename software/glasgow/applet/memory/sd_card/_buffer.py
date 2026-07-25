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

    def __init__(self, *, boot_data=None):
        if boot_data is not None:
            if len(boot_data) != BUFFER_SIZE:
                raise ValueError(f"boot buffer must be exactly {BUFFER_SIZE} bytes")
            self._boot_init = [
                int.from_bytes(boot_data[offset:offset + 8], "little")
                for offset in range(0, BUFFER_SIZE, 8)
            ]
        else:
            self._boot_init = None
        super().__init__({
            "host": In(BlockBufferPortSignature()),
            "data": In(BlockBufferPortSignature()),
            "restore_boot": In(1),
            "boot_restored": Out(1),
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
        restore_active = Signal()
        restore_valid = Signal()
        restore_read_address = Signal(range(BUFFER_SIZE // 8))
        restore_write_address = Signal(range(BUFFER_SIZE // 8))
        if self._boot_init is not None:
            m.submodules.boot_memory = boot_memory = memory.Memory(
                shape=64, depth=BUFFER_SIZE // 8, init=self._boot_init)
            boot_read_port = boot_memory.read_port(domain="sd")
            m.d.comb += boot_read_port.addr.eq(restore_read_address)

            with m.If(~self.restore_boot):
                m.d.sd += [
                    restore_active.eq(0),
                    restore_valid.eq(0),
                    self.boot_restored.eq(0),
                ]
            with m.Elif(~restore_active & ~self.boot_restored):
                m.d.sd += [
                    restore_active.eq(1),
                    restore_valid.eq(0),
                    restore_read_address.eq(0),
                    restore_write_address.eq(0),
                ]
            with m.Elif(restore_active):
                with m.If(restore_valid):
                    with m.If(restore_write_address == BUFFER_SIZE // 8 - 1):
                        m.d.sd += [
                            restore_active.eq(0),
                            self.boot_restored.eq(1),
                        ]
                    with m.Else():
                        m.d.sd += [
                            restore_write_address.eq(restore_write_address + 1),
                        ]
                        with m.If(restore_read_address < BUFFER_SIZE // 8 - 1):
                            m.d.sd += restore_read_address.eq(
                                restore_read_address + 1)
                with m.Else():
                    m.d.sd += [
                        restore_valid.eq(1),
                        restore_read_address.eq(1),
                    ]

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
        restore_write_grant = restore_active & restore_valid
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
            write_port.en.eq(Mux(restore_write_grant, C(0xff, 8),
                Mux(host_write_grant, self.host.write_en,
                    Mux(data_write_grant, self.data.write_en, 0)))),
            write_port.addr.eq(Mux(
                restore_write_grant, restore_write_address,
                Mux(host_write_grant, self.host.write_address[3:],
                    self.data.write_address[3:]))),
            write_port.data.eq(Mux(
                restore_write_grant,
                boot_read_port.data if self._boot_init is not None else 0,
                Mux(host_write_grant, self.host.write_data, self.data.write_data))),
            read_port.addr.eq(Mux(
                host_read_grant, self.host.read_address[3:],
                Mux(data_read_grant, self.data.read_address[3:], 0))),
            self.host.read_data.eq(read_port.data.word_select(read_byte_select, 8)),
            self.data.read_data.eq(read_port.data.word_select(read_byte_select, 8)),
        ]

        return m
