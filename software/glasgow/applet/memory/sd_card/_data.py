from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.wiring import In, Out

from ._buffer import BlockBufferPortSignature
from ._gateware_support import (
    BLOCK_SIZE, BUFFER_BLOCKS, IMAGE_SIZE_WIDTH, CardState, DataMode,
)
from ._interfaces import CommandDataSignature, DataHostSignature
from ._lanes import SDDataLaneCodec
from ._read_buffer import ReadBufferManager


__all__ = ["READ_NAC_CLOCKS", "DataEngine"]


# Leave a small card access-time (Nac) interval between the final R1 response bit and the data
# start token. A zero-gap response is legal in the abstract protocol model but is unlike a
# physical card and can race legacy host-controller software while it consumes the response.
# Keep this much shorter than the PXA25x driver's first-data timeout.
READ_NAC_CLOCKS = 8


class DataEngine(wiring.Component):
    dat_i: In(4)
    dat_o: Out(4, init=0xf)
    dat_oe: Out(4)
    scr: In(64)
    image_size: In(IMAGE_SIZE_WIDTH)
    prime_first_read: In(1)
    boot_preinitialized: In(1)
    boot_address_0: In(32)
    boot_address_1: In(32)
    boot_restored: In(1)
    restore_boot: Out(1)

    control: In(CommandDataSignature())
    storage: Out(DataHostSignature())
    buffer: Out(BlockBufferPortSignature())
    read_bank_states: Out(BUFFER_BLOCKS * 3)

    read_count: Out(32)
    write_count: Out(32)
    prefetch_waits: Out(32)
    prefetch_wait_cycles: Out(32)
    max_prefetch_wait: Out(32)
    protocol_errors: Out(16)

    def elaborate(self, platform):
        m = Module()
        m.submodules.read_buffers = read_buffers = ReadBufferManager()
        buffer_client = read_buffers.client
        buffer_host = read_buffers.host
        wiring.connect(m, buffer_host, wiring.flipped(self.storage.read))
        m.d.comb += self.read_bank_states.eq(buffer_client.bank_states)
        _elaborate_data_transfer_engine(
            m, self, self.buffer, self.dat_i, self.dat_o, self.dat_oe,
            self.control, self.storage, buffer_client)
        return m


def _elaborate_data_transfer_engine(
        m, component, block_buffer, dat_i, dat_o, dat_oe, control, storage, read_buffers):
    self = component
    data_mode = control.data_mode
    bus_width_4 = control.bus_width_4
    current_address = control.current_address
    response_active = control.response_active
    stop_requested = control.stop_requested
    mutation = storage.mutation
    write_request = mutation.write_request
    erase_request = mutation.erase_request
    host_ack_valid = mutation.ack_valid
    host_ack_status = mutation.ack_status
    host_ack_consume = mutation.ack_consume
    read_error_pending = Signal()
    # Start only after a complete, CRC-valid CMD0. External CLK can be intermittent while the
    # target is off or the applet is being configured, which is not enough to frame a host event.
    boot_prime_active = Signal()
    boot_prime_started = Signal()
    m.d.comb += [
        read_buffers.reset.eq(
            stop_requested | ((data_mode == DataMode.Idle) & ~boot_prime_active)),
        read_buffers.preload_boot.eq(0),
        read_buffers.preload_boot_address_0.eq(self.boot_address_0),
        read_buffers.preload_boot_address_1.eq(self.boot_address_1),
        read_buffers.lookup_address.eq(current_address),
        read_buffers.reserve.eq(0),
        read_buffers.reserve_address.eq(0),
        read_buffers.reserve_blocks.eq(Mux(data_mode == DataMode.ReadMulti, 2, 1)),
        read_buffers.discard_stale.eq(0),
        read_buffers.activate.eq(0),
        read_buffers.release.eq(0),
        read_buffers.mutation_acquire.eq(0),
        read_buffers.mutation_release.eq(0),
        mutation.write_bank.eq(read_buffers.mutation_bank),
        self.restore_boot.eq(0),
    ]

    m.d.comb += [
        control.state_update_valid.eq(0),
        control.state_update.eq(CardState.Idle),
        control.mode_update_valid.eq(0),
        control.mode_update.eq(DataMode.Idle),
        control.address_update_valid.eq(0),
        control.address_update.eq(0),
    ]

    def update_state(value):
        m.d.comb += [control.state_update_valid.eq(1), control.state_update.eq(value)]

    def update_mode(value):
        m.d.comb += [control.mode_update_valid.eq(1), control.mode_update.eq(value)]

    def update_address(value):
        m.d.comb += [
            control.address_update_valid.eq(1),
            control.address_update.eq(value),
        ]

    with m.If(mutation.write_request_accepted | stop_requested |
              (data_mode == DataMode.Idle)):
        m.d.sd += write_request.eq(0)
    with m.If(mutation.erase_request_accepted):
        m.d.sd += erase_request.eq(0)
    with m.If(read_buffers.reply_fault):
        m.d.sd += read_error_pending.eq(1)
    # Start the boot-bank fill as soon as the coherent configuration snapshot reaches the SD
    # domain. This uses the plentiful identification clocks before the legacy host starts its
    # first, very short CMD18 data timer. CMD0 re-arms the same fill for warm target resets.
    with m.If(component.prime_first_read & ~boot_prime_started):
        m.d.sd += [
            boot_prime_active.eq(1),
            boot_prime_started.eq(1),
        ]
    with m.Elif(control.card_reset & component.prime_first_read):
        m.d.sd += boot_prime_active.eq(1)
    with m.Elif(data_mode != DataMode.Idle):
        m.d.sd += boot_prime_active.eq(0)

    # Data transfer engine. It never depends on a full image being resident on the FPGA: reads
    # ping-pong between two blocks, and writes drain one block at a time.
    data_byte = Signal(range(BLOCK_SIZE))
    data_bit = Signal(range(16))
    high_nibble = Signal(4)
    write_shift = Signal(8)
    crc_lanes = Array(Signal(16, name=f"crc_lane_{index}") for index in range(4))
    received_crc = Array(Signal(16, name=f"received_crc_{index}") for index in range(4))
    data_response = Signal(5)
    data_response_bits = Signal(range(6))
    write_accepted = Signal()
    read_advance = Signal()
    inter_block_gap = Signal(range(65))
    register_read = ((data_mode == DataMode.ReadSCR) |
                     (data_mode == DataMode.ReadSDStatus))
    valid_read = ((data_mode == DataMode.ReadSingle) |
                  (data_mode == DataMode.ReadMulti) |
                  register_read)
    lanes = SDDataLaneCodec(
        bus_width_4=bus_width_4, dat_i=dat_i, bit_index=data_bit,
        crc_lanes=crc_lanes, received_crc=received_crc)

    m.d.comb += [
        dat_o.eq(0xf),
        dat_oe.eq(0),
        host_ack_consume.eq(0),
        read_advance.eq(0),
        block_buffer.write_en.eq(0),
        block_buffer.write_address.eq(0),
        block_buffer.write_data.eq(0),
        block_buffer.read_active.eq(read_buffers.active),
        block_buffer.read_address.eq(Cat(
            Mux(read_advance, data_byte + 1, data_byte)[:9], read_buffers.active_bank)),
    ]
    m.d.sd += Assert(~(block_buffer.write_en.any() & block_buffer.read_active))

    with m.FSM(domain="sd", name="data_transfer"):
        with m.State("Idle"):
            with m.If(read_error_pending | read_buffers.reply_fault):
                m.d.sd += [
                    read_error_pending.eq(0),
                    self.protocol_errors.eq(self.protocol_errors + 1),
                ]
                update_mode(DataMode.Idle)
                update_state(CardState.Tran)
            with m.Else():
                with m.If((data_mode == DataMode.Idle) & boot_prime_active):
                    # Refill the first two sectors as soon as card initialization supplies
                    # clocks. MAINTE starts with CMD18 at address zero and cannot tolerate a
                    # cold USB round trip before its first data token. Ready banks survive the
                    # ordinary idle reset and are consumed by that first transfer. CMD0 re-arms
                    # this path for every target reboot even when Glasgow stays powered.
                    m.d.comb += read_buffers.lookup_address.eq(0)
                    with m.If(self.boot_preinitialized):
                        m.d.comb += self.restore_boot.eq(1)
                        with m.If(self.boot_restored):
                            m.d.comb += read_buffers.preload_boot.eq(1)
                            m.d.sd += boot_prime_active.eq(0)
                    with m.Else():
                        reserve_address = read_buffers.ready_ahead * BLOCK_SIZE
                        m.d.comb += [
                            read_buffers.reserve_address.eq(reserve_address),
                            read_buffers.reserve_blocks.eq(2),
                        ]
                        with m.If(read_buffers.reserve_ready &
                                  (read_buffers.ready_ahead < BUFFER_BLOCKS)):
                            m.d.comb += read_buffers.reserve.eq(1)
                        with m.If(read_buffers.ready_ahead == BUFFER_BLOCKS):
                            m.d.sd += boot_prime_active.eq(0)
                with m.Else():
                    with m.Switch(data_mode):
                        with m.Case(DataMode.ReadSingle, DataMode.ReadMulti):
                            m.d.comb += read_buffers.discard_stale.eq(1)
                            reserve_address = current_address + \
                                read_buffers.ready_ahead * BLOCK_SIZE
                            m.d.comb += read_buffers.reserve_address.eq(reserve_address)
                            with m.If(read_buffers.reserve_ready &
                                      (read_buffers.ready_ahead < BUFFER_BLOCKS) &
                                      (reserve_address <= self.image_size - BLOCK_SIZE)):
                                m.d.comb += read_buffers.reserve.eq(1)
                            with m.If(~response_active & read_buffers.ready &
                                      (read_buffers.ready_ahead >= 1)):
                                m.d.comb += read_buffers.activate.eq(1)
                                m.d.sd += [
                                    data_byte.eq(0),
                                    data_bit.eq(READ_NAC_CLOCKS),
                                ]
                                for lane in range(4):
                                    m.d.sd += crc_lanes[lane].eq(0)
                                m.next = "Read-Nac"
                        with m.Case(DataMode.ReadSCR, DataMode.ReadSDStatus):
                            with m.If(~response_active):
                                m.d.sd += [
                                    data_byte.eq(0),
                                    data_bit.eq(READ_NAC_CLOCKS),
                                ]
                                for lane in range(4):
                                    m.d.sd += crc_lanes[lane].eq(0)
                                m.next = "Read-Nac"
                        with m.Case(DataMode.WriteSingle, DataMode.WriteMulti):
                            m.d.comb += read_buffers.mutation_acquire.eq(1)
                            with m.If(~response_active & read_buffers.mutation_active):
                                m.next = "Write-Wait-Start"
                        with m.Case(DataMode.Erase):
                            with m.If(~response_active):
                                m.d.sd += erase_request.eq(1)
                                m.next = "Busy"

        with m.State("Read-Nac"):
            # Keep DAT released while the host finishes consuming the command response. This is
            # the initial read-data access interval; Read-Gap separately handles CMD18 blocks.
            with m.If(stop_requested | ~valid_read):
                m.next = "Idle"
            with m.Elif(data_bit == 0):
                m.next = "Read-Start"
            with m.Else():
                # data_bit is otherwise idle between the response and the first data bit, so
                # reusing it avoids another counter on the nearly full iCE40.
                m.d.sd += data_bit.eq(data_bit - 1)

        with m.State("Read-Start"):
            m.d.comb += [
                dat_o.eq(lanes.start_symbol),
                dat_oe.eq(lanes.output_enable),
            ]
            # Fetch the next aligned sector pair while the active bank is being shifted out.
            # The host engine tags the speculative second packet and drains it on a nonsequential
            # request, so an early CMD12 cannot relabel surplus data.
            m.d.comb += [
                read_buffers.reserve_address.eq(current_address + BLOCK_SIZE),
                read_buffers.reserve_blocks.eq(2),
            ]
            with m.If((data_mode == DataMode.ReadMulti) & read_buffers.reserve_ready &
                      (current_address <= self.image_size - (2 * BLOCK_SIZE))):
                m.d.comb += read_buffers.reserve.eq(1)
            m.next = "Read-Data"
            with m.If(stop_requested |
                      ~valid_read):
                m.next = "Idle"

        with m.State("Read-Data"):
            source_byte = Mux(
                data_mode == DataMode.ReadSCR,
                self.scr.word_select(data_byte[:3] ^ 0b111, 8),
                Mux(data_mode == DataMode.ReadSDStatus, C(0, 8),
                    block_buffer.read_data))
            symbol = lanes.read_symbol(source_byte)
            m.d.comb += [dat_o.eq(symbol), dat_oe.eq(lanes.output_enable)]
            lanes.advance_read_crc(m, symbol)
            with m.If(lanes.byte_done):
                m.d.sd += data_bit.eq(0)
                block_last = Mux(data_mode == DataMode.ReadSCR,
                    data_byte == 7,
                    Mux(data_mode == DataMode.ReadSDStatus,
                        data_byte == 63, data_byte == BLOCK_SIZE - 1))
                with m.If(block_last):
                    m.d.sd += data_bit.eq(15)
                    m.next = "Read-CRC"
                with m.Else():
                    m.d.comb += read_advance.eq(1)
                    m.d.sd += data_byte.eq(data_byte + 1)
            with m.Else():
                m.d.sd += data_bit.eq(data_bit + 1)
            with m.If(stop_requested |
                      ~valid_read):
                m.next = "Idle"

        with m.State("Read-CRC"):
            m.d.comb += [
                dat_o.eq(lanes.read_crc_symbol()),
                dat_oe.eq(lanes.output_enable),
            ]
            with m.If(data_bit == 0):
                m.next = "Read-End"
            with m.Else():
                m.d.sd += data_bit.eq(data_bit - 1)
            with m.If(stop_requested |
                      ~valid_read):
                m.next = "Idle"

        with m.State("Read-End"):
            m.d.comb += [
                dat_o.eq(lanes.end_symbol),
                dat_oe.eq(lanes.output_enable),
            ]
            m.d.sd += self.read_count.eq(self.read_count + 1)
            with m.If((data_mode == DataMode.ReadSingle) |
                      (data_mode == DataMode.ReadMulti)):
                m.d.comb += read_buffers.release.eq(1)
            with m.If(data_mode == DataMode.ReadMulti):
                with m.If(current_address <= self.image_size - (2 * BLOCK_SIZE)):
                    next_address = current_address + BLOCK_SIZE
                    m.d.comb += read_buffers.lookup_address.eq(next_address)
                    next_ready = read_buffers.ready | (
                        storage.read.reply_ready &
                        (storage.read.reply_address == next_address))
                    with m.If(~next_ready):
                        m.d.sd += self.prefetch_waits.eq(self.prefetch_waits + 1)
                    update_address(current_address + BLOCK_SIZE)
                with m.Else():
                    m.d.sd += self.protocol_errors.eq(self.protocol_errors + 1)
                    update_mode(DataMode.Idle)
                    update_state(CardState.Tran)
                m.d.sd += inter_block_gap.eq(64)
                m.next = "Read-Gap"
            with m.Else():
                update_mode(DataMode.Idle)
                update_state(CardState.Tran)
                m.next = "Idle"
            with m.If(stop_requested |
                      ~valid_read):
                m.next = "Idle"

        with m.State("Read-Gap"):
            # Leave at least one complete command frame between blocks. The legacy Sharp PXA25x
            # stack can then issue CMD12 without racing the following data token. Longer fixed
            # gaps starve its genuine multi-block transfers, so host compatibility is obtained by
            # advertising a conservative transfer rate instead of extending this Nac interval.
            with m.If(stop_requested | (data_mode != DataMode.ReadMulti)):
                m.next = "Idle"
            with m.Elif(inter_block_gap == 0):
                m.next = "Idle"
            with m.Else():
                m.d.sd += inter_block_gap.eq(inter_block_gap - 1)

        with m.State("Write-Wait-Start"):
            with m.If(stop_requested |
                      ((data_mode != DataMode.WriteSingle) &
                       (data_mode != DataMode.WriteMulti))):
                m.next = "Idle"
            with m.Elif(lanes.start_seen):
                m.d.sd += [
                    data_byte.eq(0),
                    data_bit.eq(0),
                    write_shift.eq(0),
                ]
                for lane in range(4):
                    m.d.sd += [
                        crc_lanes[lane].eq(0),
                        received_crc[lane].eq(0),
                    ]
                m.next = "Write-Data"

        with m.State("Write-Data"):
            received_octet = lanes.write_octet(high_nibble, write_shift)
            lanes.advance_write_data(m, high_nibble, write_shift)
            with m.If(lanes.byte_done):
                m.d.comb += [
                    block_buffer.write_en.eq(1 << data_byte[:3]),
                    block_buffer.write_address.eq(Cat(data_byte[:9], read_buffers.mutation_bank)),
                    block_buffer.write_data.eq(Cat(*([received_octet] * 8))),
                ]
                m.d.sd += data_bit.eq(0)
                with m.If(data_byte == BLOCK_SIZE - 1):
                    m.next = "Write-CRC"
                with m.Else():
                    m.d.sd += data_byte.eq(data_byte + 1)
            with m.Else():
                m.d.sd += data_bit.eq(data_bit + 1)

        with m.State("Write-CRC"):
            lanes.capture_received_crc(m)
            with m.If(data_bit == 15):
                m.next = "Write-End"
            with m.Else():
                m.d.sd += data_bit.eq(data_bit + 1)

        with m.State("Write-End"):
            crc_ok = lanes.received_crc_valid()
            end_ok = lanes.end_seen
            m.d.sd += [
                data_response.eq(Mux(crc_ok & end_ok, 0b00101, 0b01011)),
                data_response_bits.eq(5),
                write_accepted.eq(crc_ok & end_ok),
                data_bit.eq(0),
            ]
            m.next = "Write-Response-Delay"

        with m.State("Write-Response-Delay"):
            with m.If(data_bit == 1):
                m.next = "Write-Response"
            with m.Else():
                m.d.sd += data_bit.eq(data_bit + 1)

        with m.State("Write-Response"):
            m.d.comb += [
                dat_o.eq(Cat(data_response[4], C(0b111, 3))),
                dat_oe.eq(0x1),
            ]
            m.d.sd += data_response.eq(data_response << 1)
            with m.If(data_response_bits == 1):
                m.d.sd += data_response_bits.eq(0)
                with m.If(write_accepted):
                    m.d.sd += write_request.eq(1)
                    update_state(CardState.Prg)
                    m.next = "Busy"
                with m.Else():
                    update_mode(DataMode.Idle)
                    update_state(CardState.Tran)
                    m.next = "Idle"
            with m.Else():
                m.d.sd += data_response_bits.eq(data_response_bits - 1)

        with m.State("Busy"):
            m.d.comb += [dat_o.eq(0xe), dat_oe.eq(0x1)]
            with m.If(host_ack_valid):
                m.d.comb += host_ack_consume.eq(1)
                with m.If((host_ack_status == 0) &
                          (data_mode == DataMode.WriteMulti)):
                    m.d.sd += self.write_count.eq(self.write_count + 1)
                    with m.If(current_address <= self.image_size - (2 * BLOCK_SIZE)):
                        update_address(current_address + BLOCK_SIZE)
                        update_state(CardState.Rcv)
                        m.next = "Write-Wait-Start"
                    with m.Else():
                        m.d.sd += self.protocol_errors.eq(self.protocol_errors + 1)
                        update_mode(DataMode.Idle)
                        update_state(CardState.Tran)
                        m.next = "Idle"
                with m.Else():
                    with m.If((host_ack_status == 0) &
                              (data_mode == DataMode.WriteSingle)):
                        m.d.sd += self.write_count.eq(self.write_count + 1)
                    with m.If(host_ack_status != 0):
                        m.d.sd += self.protocol_errors.eq(self.protocol_errors + 1)
                    update_mode(DataMode.Idle)
                    update_state(CardState.Tran)
                    m.next = "Idle"

    with m.If(read_buffers.mutation_active &
              (stop_requested |
               ((data_mode != DataMode.WriteSingle) &
                (data_mode != DataMode.WriteMulti)))):
        m.d.comb += read_buffers.mutation_release.eq(1)
