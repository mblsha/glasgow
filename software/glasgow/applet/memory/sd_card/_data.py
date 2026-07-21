from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.wiring import In, Out

from ._buffer import BlockBufferPortSignature
from ._gateware_support import BLOCK_SIZE, CardState, DataMode, crc16_value as _crc16_value
from ._interfaces import CommandDataSignature, DataHostSignature


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

    read_count: Out(32)
    write_count: Out(32)
    protocol_errors: Out(16)

    def elaborate(self, platform):
        m = Module()
        _elaborate_data_transfer_engine(
            m, self, self.buffer, self.dat_i, self.dat_o, self.dat_oe,
            self.control, self.storage)
        return m


def _elaborate_data_transfer_engine(
        m, component, block_buffer, dat_i, dat_o, dat_oe, control, storage):
    self = component
    data_mode = control.data_mode
    bus_width_4 = control.bus_width_4
    current_address = control.current_address
    response_active = control.response_active
    stop_requested = control.stop_requested
    read_request = storage.read_request
    write_request = storage.write_request
    erase_request = storage.erase_request
    request_sent = Signal()
    read_buffer_ready = storage.read_buffer_ready
    read_buffer_address = storage.read_buffer_address
    read_buffer_consume = storage.read_buffer_consume
    read_reply_error = storage.read_reply_error
    host_ack_valid = storage.host_ack_valid
    host_ack_status = storage.host_ack_status
    host_ack_consume = storage.host_ack_consume

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

    with m.If(storage.read_request_accepted | stop_requested |
              (data_mode == DataMode.Idle)):
        m.d.sd += read_request.eq(0)
    with m.If(storage.write_request_accepted | stop_requested |
              (data_mode == DataMode.Idle)):
        m.d.sd += write_request.eq(0)
    with m.If(storage.erase_request_accepted):
        m.d.sd += erase_request.eq(0)
    # Data transfer engine. It never depends on a full image being resident on the FPGA: one
    # block is refilled or drained for each SD data packet.
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

    m.d.comb += [
        dat_o.eq(0xf),
        dat_oe.eq(0),
        read_buffer_consume.eq(0),
        host_ack_consume.eq(0),
        read_advance.eq(0),
        block_buffer.write_en.eq(0),
        block_buffer.write_address.eq(0),
        block_buffer.write_data.eq(0),
        block_buffer.read_active.eq(
            (data_mode == DataMode.ReadSingle) | (data_mode == DataMode.ReadMulti)),
        block_buffer.read_address.eq(Mux(read_advance, data_byte + 1, data_byte)),
    ]

    with m.FSM(domain="sd", name="data_transfer"):
        with m.State("Idle"):
            with m.If(read_reply_error):
                m.d.sd += [
                    request_sent.eq(0),
                    self.protocol_errors.eq(self.protocol_errors + 1),
                ]
                update_mode(DataMode.Idle)
                update_state(CardState.Tran)
            with m.Else():
                with m.Switch(data_mode):
                    with m.Case(DataMode.ReadSingle, DataMode.ReadMulti):
                        with m.If(~request_sent):
                            m.d.sd += [
                                read_request.eq(1),
                                request_sent.eq(1),
                            ]
                        with m.If(~response_active & read_buffer_ready &
                                  (read_buffer_address == current_address)):
                            m.d.comb += read_buffer_consume.eq(1)
                            m.d.sd += [
                                data_byte.eq(0),
                                data_bit.eq(0),
                            ]
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
                    with m.Default():
                        m.d.sd += request_sent.eq(0)

        with m.State("Read-Start"):
            with m.If(bus_width_4):
                m.d.comb += [dat_o.eq(0), dat_oe.eq(0xf)]
            with m.Else():
                m.d.comb += [dat_o.eq(0xe), dat_oe.eq(0x1)]
            m.next = "Read-Data"

        with m.State("Read-Data"):
            source_byte = Mux(
                data_mode == DataMode.ReadSCR,
                self.scr.word_select(data_byte[:3] ^ 0b111, 8),
                block_buffer.read_data)
            with m.If(bus_width_4):
                nibble = Mux(data_bit == 0, source_byte[4:8], source_byte[0:4])
                m.d.comb += [dat_o.eq(nibble), dat_oe.eq(0xf)]
                for lane in range(4):
                    m.d.sd += crc_lanes[lane].eq(_crc16_value(crc_lanes[lane], nibble[lane]))
                with m.If(data_bit == 1):
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
                    m.d.sd += data_bit.eq(1)
            with m.Else():
                bit_value = source_byte.bit_select(data_bit[:3] ^ 0b111, 1)
                m.d.comb += [dat_o.eq(Cat(bit_value, C(0b111, 3))), dat_oe.eq(0x1)]
                m.d.sd += crc_lanes[0].eq(_crc16_value(crc_lanes[0], bit_value))
                with m.If(data_bit == 7):
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
            with m.If(bus_width_4):
                crc_bits = Cat(*(crc_lanes[lane].bit_select(data_bit, 1) for lane in range(4)))
                m.d.comb += [dat_o.eq(crc_bits), dat_oe.eq(0xf)]
            with m.Else():
                m.d.comb += [
                    dat_o.eq(Cat(crc_lanes[0].bit_select(data_bit, 1), C(0b111, 3))),
                    dat_oe.eq(0x1),
                ]
            with m.If(data_bit == 0):
                m.next = "Read-End"
            with m.Else():
                m.d.sd += data_bit.eq(data_bit - 1)

        with m.State("Read-End"):
            with m.If(bus_width_4):
                m.d.comb += [dat_o.eq(0xf), dat_oe.eq(0xf)]
            with m.Else():
                m.d.comb += [dat_o.eq(0xf), dat_oe.eq(0x1)]
            m.d.sd += self.read_count.eq(self.read_count + 1)
            with m.If(data_mode == DataMode.ReadMulti):
                with m.If(current_address <= self.image_size - (2 * BLOCK_SIZE)):
                    m.d.sd += request_sent.eq(0)
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
            start_seen = Mux(bus_width_4, dat_i == 0, dat_i[0] == 0)
            with m.If(stop_requested |
                      ((data_mode != DataMode.WriteSingle) &
                       (data_mode != DataMode.WriteMulti))):
                m.next = "Idle"
            with m.Elif(start_seen):
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
            with m.If(bus_width_4):
                for lane in range(4):
                    m.d.sd += crc_lanes[lane].eq(_crc16_value(crc_lanes[lane], dat_i[lane]))
                with m.If(data_bit == 0):
                    m.d.sd += [
                        high_nibble.eq(dat_i),
                        data_bit.eq(1),
                    ]
                with m.Else():
                    m.d.comb += [
                        block_buffer.write_en.eq(1),
                        block_buffer.write_address.eq(data_byte),
                        block_buffer.write_data.eq(Cat(dat_i, high_nibble)),
                    ]
                    m.d.sd += data_bit.eq(0)
                    with m.If(data_byte == BLOCK_SIZE - 1):
                        m.d.sd += data_bit.eq(0)
                        m.next = "Write-CRC"
                    with m.Else():
                        m.d.sd += data_byte.eq(data_byte + 1)
            with m.Else():
                next_octet = Cat(dat_i[0], write_shift[:7])
                m.d.sd += [
                    write_shift.eq(next_octet),
                    crc_lanes[0].eq(_crc16_value(crc_lanes[0], dat_i[0])),
                ]
                with m.If(data_bit == 7):
                    m.d.comb += [
                        block_buffer.write_en.eq(1),
                        block_buffer.write_address.eq(data_byte),
                        block_buffer.write_data.eq(next_octet),
                    ]
                    m.d.sd += data_bit.eq(0)
                    with m.If(data_byte == BLOCK_SIZE - 1):
                        m.next = "Write-CRC"
                    with m.Else():
                        m.d.sd += data_byte.eq(data_byte + 1)
                with m.Else():
                    m.d.sd += data_bit.eq(data_bit + 1)

        with m.State("Write-CRC"):
            for lane in range(4):
                with m.If(bus_width_4 | (lane == 0)):
                    m.d.sd += received_crc[lane].eq(
                        Cat(dat_i[lane], received_crc[lane][:15]))
            with m.If(data_bit == 15):
                m.next = "Write-End"
            with m.Else():
                m.d.sd += data_bit.eq(data_bit + 1)

        with m.State("Write-End"):
            crc_ok = (received_crc[0] == crc_lanes[0])
            for lane in range(1, 4):
                crc_ok &= ~bus_width_4 | (received_crc[lane] == crc_lanes[lane])
            end_ok = Mux(bus_width_4, dat_i == 0xf, dat_i[0] == 1)
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
