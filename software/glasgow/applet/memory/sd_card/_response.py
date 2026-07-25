from amaranth import *

from ._gateware_support import OCR, RCA, crc7_value


__all__ = ["RESPONSE_GUARD_CLOCKS", "ResponseEngine"]


# Use the complete legacy-compatible command-to-response window. Including the serializer's load
# cycle, seven explicit guard cycles produce eight driven-idle samples before the response start.
RESPONSE_GUARD_CLOCKS = 7


class ResponseEngine:
    """Serializer for SD R1/R2/R3/R6 command responses."""

    def __init__(self):
        self.shift = Signal(136)
        self.bits = Signal(range(137))
        self.has_crc = Signal()
        self.crc = Signal(7)
        self.crc_bits = Signal(range(8))
        self.end = Signal()
        self.pending = Signal(2)
        self.turnaround_guard = Signal(range(RESPONSE_GUARD_CLOCKS + 1))
        self.pending_prefix = Signal(40)
        self.pending_data = Signal(136)
        self.driving = (self.bits != 0) | (self.crc_bits != 0) | self.end
        self.active = self.driving | (self.pending != 0)

    def connect(self, m, cmd_o, cmd_oe, enabled):
        output_next = Mux(
            self.bits != 0, self.shift[135],
            Mux(self.crc_bits != 0, self.crc[6], 1))
        output = Signal(init=1)
        output_enable = Signal()
        m.d.sd_fall += [
            output.eq(output_next),
            # Assert the card side of the bidirectional CMD buffer while the response is still
            # pending and the driven value is idle high. On the following falling edge the start
            # bit is driven low. This gives the external level translator a complete half-cycle
            # to turn around before the host samples the first response bit. Without this
            # pre-drive, output-enable and the start bit changed together; the PXA255 could miss
            # the start of the first high-speed R1 after identification and decode shifted status
            # bits as CARD_IS_LOCKED/LOCK_UNLOCK_FAILED.
            output_enable.eq(self.active & enabled),
        ]
        m.d.comb += [
            cmd_o.eq(output),
            cmd_oe.eq(output_enable),
        ]

    def load_r1(self, m, command, status):
        m.d.sd += [
            self.pending.eq(1),
            self.turnaround_guard.eq(RESPONSE_GUARD_CLOCKS),
            self.pending_prefix[:32].eq(status),
            self.pending_prefix[32:38].eq(command),
            self.pending_prefix[38:40].eq(0),
        ]

    def load_r2(self, m, register):
        response = Cat(C(1, 1), register[1:128], C(0x3f, 6), C(0, 2))
        m.d.sd += [
            self.pending.eq(3),
            self.turnaround_guard.eq(RESPONSE_GUARD_CLOCKS),
            self.pending_data.eq(response),
        ]

    def load_r3(self, m):
        response = Cat(C(1, 1), C(0x7f, 7), C(OCR, 32), C(0x3f, 6), C(0, 2))
        m.d.sd += [
            self.pending.eq(2),
            self.turnaround_guard.eq(RESPONSE_GUARD_CLOCKS),
            self.pending_data.eq(response),
        ]

    def load_r6(self, m, status):
        r6_status = Cat(status[:13], status[19], status[22], status[23])
        argument = Cat(r6_status, C(RCA, 16))
        m.d.sd += [
            self.pending.eq(1),
            self.turnaround_guard.eq(RESPONSE_GUARD_CLOCKS),
            self.pending_prefix[:32].eq(argument),
            self.pending_prefix[32:38].eq(3),
            self.pending_prefix[38:40].eq(0),
        ]

    def elaborate(self, m):
        with m.If(self.bits != 0):
            m.d.sd += [
                self.shift.eq(self.shift << 1),
                self.bits.eq(self.bits - 1),
            ]
            with m.If(self.has_crc):
                m.d.sd += self.crc.eq(crc7_value(self.crc, self.shift[135]))
                with m.If(self.bits == 1):
                    m.d.sd += self.crc_bits.eq(7)
        with m.Elif(self.crc_bits != 0):
            m.d.sd += [
                self.crc.eq(self.crc << 1),
                self.crc_bits.eq(self.crc_bits - 1),
            ]
            with m.If(self.crc_bits == 1):
                m.d.sd += self.end.eq(1)
        with m.Elif(self.end):
            m.d.sd += [
                self.end.eq(0),
                self.has_crc.eq(0),
            ]
        with m.Elif((self.pending != 0) & (self.turnaround_guard != 0)):
            # Keep owning CMD at idle high through the legal Ncr response window. This gives a
            # legacy controller time to arm response capture and makes translator turnaround
            # independent of route-dependent output-enable delay.
            m.d.sd += self.turnaround_guard.eq(self.turnaround_guard - 1)
        with m.Elif(self.pending != 0):
            with m.Switch(self.pending):
                with m.Case(1):
                    m.d.sd += [
                        self.shift.eq(self.pending_prefix << 96),
                        self.bits.eq(40),
                        self.has_crc.eq(1),
                        self.crc.eq(0),
                    ]
                with m.Case(2):
                    m.d.sd += [
                        self.shift.eq(self.pending_data << 88),
                        self.bits.eq(48),
                        self.has_crc.eq(0),
                    ]
                with m.Case(3):
                    m.d.sd += [
                        self.shift.eq(self.pending_data),
                        self.bits.eq(136),
                        self.has_crc.eq(0),
                    ]
            m.d.sd += self.pending.eq(0)

    def reset(self):
        return [
            self.bits.eq(0),
            self.crc_bits.eq(0),
            self.end.eq(0),
            self.pending.eq(0),
            self.turnaround_guard.eq(0),
        ]
