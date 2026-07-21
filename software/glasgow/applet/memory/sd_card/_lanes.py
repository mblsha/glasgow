from amaranth import *

from ._gateware_support import crc16_value


__all__ = ["SDDataLaneCodec"]


class SDDataLaneCodec:
    """Combinational and sequential helpers for SD 1-bit/4-bit data symbols.

    The surrounding transfer FSM owns byte and bit counters; this codec owns the mapping between
    those counters, DAT symbols, reconstructed bytes, and per-lane CRC state. Keeping that mapping
    here ensures reads and writes use exactly the same bus-width conventions.
    """

    def __init__(self, *, bus_width_4, dat_i, bit_index, crc_lanes, received_crc):
        self.bus_width_4 = bus_width_4
        self.dat_i = dat_i
        self.bit_index = bit_index
        self.crc_lanes = crc_lanes
        self.received_crc = received_crc

    @property
    def output_enable(self):
        return Mux(self.bus_width_4, C(0xf, 4), C(0x1, 4))

    @property
    def start_symbol(self):
        return Mux(self.bus_width_4, C(0x0, 4), C(0xe, 4))

    @property
    def end_symbol(self):
        return C(0xf, 4)

    @property
    def start_seen(self):
        return Mux(self.bus_width_4, self.dat_i == 0, self.dat_i[0] == 0)

    @property
    def end_seen(self):
        return Mux(self.bus_width_4, self.dat_i == 0xf, self.dat_i[0] == 1)

    @property
    def byte_done(self):
        return Mux(self.bus_width_4, self.bit_index == 1, self.bit_index == 7)

    def read_symbol(self, octet):
        nibble = Mux(self.bit_index == 0, octet[4:8], octet[0:4])
        bit = octet.bit_select(self.bit_index[:3] ^ 0b111, 1)
        return Mux(self.bus_width_4, nibble, Cat(bit, C(0b111, 3)))

    def advance_read_crc(self, m, symbol):
        with m.If(self.bus_width_4):
            for lane in range(4):
                m.d.sd += self.crc_lanes[lane].eq(
                    crc16_value(self.crc_lanes[lane], symbol[lane]))
        with m.Else():
            m.d.sd += self.crc_lanes[0].eq(
                crc16_value(self.crc_lanes[0], symbol[0]))

    def read_crc_symbol(self):
        four_bit = Cat(*(
            self.crc_lanes[lane].bit_select(self.bit_index, 1) for lane in range(4)))
        one_bit = Cat(
            self.crc_lanes[0].bit_select(self.bit_index, 1), C(0b111, 3))
        return Mux(self.bus_width_4, four_bit, one_bit)

    def write_octet(self, high_nibble, write_shift):
        four_bit = Cat(self.dat_i, high_nibble)
        one_bit = Cat(self.dat_i[0], write_shift[:7])
        return Mux(self.bus_width_4, four_bit, one_bit)

    def advance_write_data(self, m, high_nibble, write_shift):
        with m.If(self.bus_width_4):
            for lane in range(4):
                m.d.sd += self.crc_lanes[lane].eq(
                    crc16_value(self.crc_lanes[lane], self.dat_i[lane]))
            with m.If(~self.byte_done):
                m.d.sd += high_nibble.eq(self.dat_i)
        with m.Else():
            next_octet = Cat(self.dat_i[0], write_shift[:7])
            m.d.sd += [
                write_shift.eq(next_octet),
                self.crc_lanes[0].eq(
                    crc16_value(self.crc_lanes[0], self.dat_i[0])),
            ]

    def capture_received_crc(self, m):
        for lane in range(4):
            with m.If(self.bus_width_4 | (lane == 0)):
                m.d.sd += self.received_crc[lane].eq(
                    Cat(self.dat_i[lane], self.received_crc[lane][:15]))

    def received_crc_valid(self):
        valid = self.received_crc[0] == self.crc_lanes[0]
        for lane in range(1, 4):
            valid &= ~self.bus_width_4 | (self.received_crc[lane] == self.crc_lanes[lane])
        return valid
