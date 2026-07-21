from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.wiring import In, Out

from ._buffer import BlockBufferPortSignature
from ._gateware_support import BLOCK_SIZE, CardState, DataMode
from ._interfaces import CommandDataSignature, DataHostSignature
from ._lanes import SDDataLaneCodec
from ._read_buffer import ReadBufferManager


__all__ = ["DataEngine"]


class DataEngine(wiring.Component):
    dat_i: In(4)
    dat_o: Out(4, init=0xf)
    dat_oe: Out(4)
    scr: In(64)
    image_size: In(25)

    control: In(CommandDataSignature())
    storage: Out(DataHostSignature())
    buffer: Out(BlockBufferPortSignature())
    read_bank_states: Out(4)

    read_count: Out(32)
    write_count: Out(32)
    prefetch_waits: Out(32)
    prefetch_wait_cycles: Out(32)
    max_prefetch_wait: Out(32)
    protocol_errors: Out(16)

    def elaborate(self, platform):
        m = Module()
        m.submodules.read_buffers = read_buffers = ReadBufferManager()
        m.d.comb += [
            self.storage.read_request.eq(read_buffers.host_request),
            self.storage.read_request_address.eq(read_buffers.host_request_address),
            self.storage.read_request_bank.eq(read_buffers.host_request_bank),
            read_buffers.host_request_accepted.eq(self.storage.read_request_accepted),
            read_buffers.host_reply_ready.eq(self.storage.read_buffer_ready),
            read_buffers.host_reply_address.eq(self.storage.read_buffer_address),
            read_buffers.host_reply_bank.eq(self.storage.read_reply_bank),
            read_buffers.host_reply_error.eq(self.storage.read_reply_error),
            self.storage.read_buffer_consume.eq(read_buffers.host_reply_consume),
            self.read_bank_states.eq(read_buffers.bank_states),
        ]
        _elaborate_data_transfer_engine(
            m, self, self.buffer, self.dat_i, self.dat_o, self.dat_oe,
            self.control, self.storage, read_buffers)
        return m


def _elaborate_data_transfer_engine(
        m, component, block_buffer, dat_i, dat_o, dat_oe, control, storage, read_buffers):
    self = component
    data_mode = control.data_mode
    bus_width_4 = control.bus_width_4
    current_address = control.current_address
    response_active = control.response_active
    stop_requested = control.stop_requested
    write_request = storage.write_request
    erase_request = storage.erase_request
    host_ack_valid = storage.host_ack_valid
    host_ack_status = storage.host_ack_status
    host_ack_consume = storage.host_ack_consume
    read_error_pending = Signal()
    prefetch_wait_active = Signal()
    prefetch_wait_current = Signal(32)
    m.d.comb += [
        read_buffers.reset.eq(stop_requested | (data_mode == DataMode.Idle)),
        read_buffers.lookup_address.eq(current_address),
        read_buffers.reserve.eq(0),
        read_buffers.reserve_address.eq(0),
        read_buffers.activate.eq(0),
        read_buffers.release.eq(0),
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

    with m.If(storage.write_request_accepted | stop_requested |
              (data_mode == DataMode.Idle)):
        m.d.sd += write_request.eq(0)
    with m.If(storage.erase_request_accepted):
        m.d.sd += erase_request.eq(0)
    with m.If(read_buffers.reply_fault):
        m.d.sd += read_error_pending.eq(1)

    with m.If(prefetch_wait_active):
        with m.If(~self.prefetch_wait_cycles.all()):
            m.d.sd += self.prefetch_wait_cycles.eq(self.prefetch_wait_cycles + 1)
        with m.If(~prefetch_wait_current.all()):
            m.d.sd += prefetch_wait_current.eq(prefetch_wait_current + 1)

    def finish_prefetch_wait():
        with m.If(prefetch_wait_current > self.max_prefetch_wait):
            m.d.sd += self.max_prefetch_wait.eq(prefetch_wait_current)
        m.d.sd += [
            prefetch_wait_active.eq(0),
            prefetch_wait_current.eq(0),
        ]

    with m.If((stop_requested | (data_mode == DataMode.Idle)) & prefetch_wait_active):
        finish_prefetch_wait()
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
    m.d.sd += Assert(~(block_buffer.write_en & block_buffer.read_active))

    with m.FSM(domain="sd", name="data_transfer"):
        with m.State("Idle"):
            with m.If(read_error_pending | read_buffers.reply_fault):
                m.d.sd += [
                    read_error_pending.eq(0),
                    self.protocol_errors.eq(self.protocol_errors + 1),
                ]
                with m.If(prefetch_wait_active):
                    finish_prefetch_wait()
                update_mode(DataMode.Idle)
                update_state(CardState.Tran)
            with m.Else():
                with m.Switch(data_mode):
                    with m.Case(DataMode.ReadSingle, DataMode.ReadMulti):
                        m.d.comb += read_buffers.reserve_address.eq(current_address)
                        with m.If(read_buffers.reserve_ready & ~read_buffers.filling &
                                  ~read_buffers.ready):
                            m.d.comb += read_buffers.reserve.eq(1)
                        with m.If(~response_active & read_buffers.ready):
                            m.d.comb += read_buffers.activate.eq(1)
                            m.d.sd += [
                                data_byte.eq(0),
                                data_bit.eq(0),
                            ]
                            with m.If(prefetch_wait_active):
                                finish_prefetch_wait()
                            for lane in range(4):
                                m.d.sd += crc_lanes[lane].eq(0)
                            m.next = "Read-Start"
                    with m.Case(DataMode.ReadSCR):
                        with m.If(~response_active):
                            m.d.sd += [
                                data_byte.eq(0),
                                data_bit.eq(0),
                            ]
                            for lane in range(4):
                                m.d.sd += crc_lanes[lane].eq(0)
                            m.next = "Read-Start"
                    with m.Case(DataMode.WriteSingle, DataMode.WriteMulti):
                        with m.If(~response_active):
                            m.next = "Write-Wait-Start"
                    with m.Case(DataMode.Erase):
                        with m.If(~response_active):
                            m.d.sd += erase_request.eq(1)
                            m.next = "Busy"

        with m.State("Read-Start"):
            m.d.comb += [
                dat_o.eq(lanes.start_symbol),
                dat_oe.eq(lanes.output_enable),
            ]
            # In multi-block mode the inactive bank is filled from the host while this bank is
            # shifted onto DAT. This hides USB/backend latency from controllers with short FIFO
            # service timeouts without retaining more than two blocks in FPGA memory.
            m.d.comb += read_buffers.reserve_address.eq(current_address + BLOCK_SIZE)
            with m.If((data_mode == DataMode.ReadMulti) & read_buffers.reserve_ready &
                      (current_address <= self.image_size - (2 * BLOCK_SIZE))):
                m.d.comb += read_buffers.reserve.eq(1)
            m.next = "Read-Data"

        with m.State("Read-Data"):
            source_byte = Mux(
                data_mode == DataMode.ReadSCR,
                self.scr.word_select(data_byte[:3] ^ 0b111, 8),
                block_buffer.read_data)
            symbol = lanes.read_symbol(source_byte)
            m.d.comb += [dat_o.eq(symbol), dat_oe.eq(lanes.output_enable)]
            lanes.advance_read_crc(m, symbol)
            with m.If(lanes.byte_done):
                m.d.sd += data_bit.eq(0)
                block_last = Mux(data_mode == DataMode.ReadSCR,
                    data_byte == 7, data_byte == BLOCK_SIZE - 1)
                with m.If(block_last):
                    m.d.sd += data_bit.eq(15)
                    m.next = "Read-CRC"
                with m.Else():
                    m.d.comb += read_advance.eq(1)
                    m.d.sd += data_byte.eq(data_byte + 1)
            with m.Else():
                m.d.sd += data_bit.eq(data_bit + 1)

        with m.State("Read-CRC"):
            m.d.comb += [
                dat_o.eq(lanes.read_crc_symbol()),
                dat_oe.eq(lanes.output_enable),
            ]
            with m.If(data_bit == 0):
                m.next = "Read-End"
            with m.Else():
                m.d.sd += data_bit.eq(data_bit - 1)

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
                        storage.read_buffer_ready &
                        (storage.read_buffer_address == next_address))
                    with m.If(~next_ready & ~prefetch_wait_active):
                        m.d.sd += [
                            self.prefetch_waits.eq(self.prefetch_waits + 1),
                            prefetch_wait_active.eq(1),
                        ]
                    update_address(current_address + BLOCK_SIZE)
                with m.Else():
                    m.d.sd += self.protocol_errors.eq(self.protocol_errors + 1)
                    update_mode(DataMode.Idle)
                    update_state(CardState.Tran)
            with m.Else():
                update_mode(DataMode.Idle)
                update_state(CardState.Tran)
            m.next = "Idle"

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
                    block_buffer.write_en.eq(1),
                    block_buffer.write_address.eq(data_byte),
                    block_buffer.write_data.eq(received_octet),
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
