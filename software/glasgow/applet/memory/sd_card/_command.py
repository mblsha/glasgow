from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.wiring import In, Out

from ._gateware_support import (
    BLOCK_SIZE, IMAGE_SIZE_WIDTH, RCA, CardState, DataMode,
    crc7_value as _crc7_value,
)
from ._interfaces import CommandDataSignature
from ._response import ResponseEngine


__all__ = ["CommandEngine"]


class CommandEngine(wiring.Component):
    cmd_i: In(1)
    cmd_o: Out(1, init=1)
    cmd_oe: Out(1)
    enabled: In(1)
    read_only: In(1)
    support_4bit: In(1)
    image_size: In(IMAGE_SIZE_WIDTH)
    cid: In(128)
    csd: In(128)

    control: Out(CommandDataSignature())

    last_command: Out(6)
    clock_count: Out(32)
    command_low_count: Out(32)
    command_count: Out(32)
    protocol_errors: Out(16)
    protocol_error_sources: Out(2)
    # Preserve the proven control build's inert interface shape. This output is
    # constant zero and has no host register or decoded-command logic.
    early_marker: Out(4)

    def __init__(self, *, capture_cmd17_wire=False):
        self._capture_cmd17_wire = capture_cmd17_wire
        super().__init__()

    def elaborate(self, platform):
        m = Module()
        _elaborate_command_engine(
            m, self, self.cmd_i, self.cmd_o, self.cmd_oe, self.enabled, self.control,
            capture_cmd17_wire=self._capture_cmd17_wire)
        return m


def _elaborate_command_engine(
        m, component, cmd_i, cmd_o, cmd_oe, enabled_sd, control, *,
        capture_cmd17_wire=False):
    self = component
    card_state = control.card_state
    data_mode = control.data_mode
    bus_width_4 = control.bus_width_4
    current_address = control.current_address
    erase_start = control.erase_start
    erase_end = control.erase_end
    erase_start_valid = Signal()
    erase_end_valid = Signal()
    app_command = Signal()

    cmd_shift = Signal(48)
    cmd_bits = Signal(range(49))
    cmd_crc_calc = Signal(7)
    cmd17_capture_bits = Signal(range(49))
    response = ResponseEngine()
    response_active = response.active

    cmd_word = Cat(cmd_i, cmd_shift[:47])
    cmd_index = cmd_word[40:46]
    cmd_argument = cmd_word[8:40]
    cmd_crc = cmd_word[1:8]

    m.d.comb += self.early_marker.eq(0)

    if capture_cmd17_wire:
        # Reuse the parser's existing 48-bit shift path while it is idle during a response. This
        # captures the pad input—not the intended serializer value—without adding a new cmd_i
        # sink that could perturb the timing being measured. Once all 48 bits are present, copy
        # the stable shift register into the ordinary clock-counter storage. Keep the capture
        # count independent of cmd_crc_calc: at the command end the latter contains CMD17's CRC7
        # value (0x2a), and relying on an overlapping reset can publish a six-clock fragment of
        # the original command instead of the complete response.
        with m.If(enabled_sd & (self.last_command == 17) & cmd_oe):
            with m.If((cmd17_capture_bits == 0) & ~cmd_i):
                m.d.sd += [
                    cmd_shift.eq(cmd_word),
                    cmd17_capture_bits.eq(1),
                ]
            with m.Elif((cmd17_capture_bits != 0) & (cmd17_capture_bits < 48)):
                with m.If(cmd17_capture_bits == 47):
                    # Publish the complete response on its final sampled bit. The host may stop
                    # SDCLK immediately after the end bit, so waiting for a 49th rising edge
                    # would leave the capture stranded in the parser shift register.
                    m.d.sd += [
                        self.clock_count.eq(cmd_word[:32]),
                        self.command_low_count.eq(cmd_word[32:48]),
                        cmd17_capture_bits.eq(48),
                    ]
                with m.Else():
                    m.d.sd += [
                        cmd_shift.eq(cmd_word),
                        cmd17_capture_bits.eq(cmd17_capture_bits + 1),
                    ]
    else:
        # These counters are deliberately kept in the SD clock domain. Besides being useful
        # diagnostics on a real host, they make it possible to distinguish a wiring or clocking
        # problem from a malformed command frame without buffering a full logic trace.
        with m.If(enabled_sd):
            m.d.sd += self.clock_count.eq(self.clock_count + 1)
            with m.If(~cmd_i):
                m.d.sd += self.command_low_count.eq(self.command_low_count + 1)

    response.connect(m, cmd_o, cmd_oe, enabled_sd)

    def status_value(*, extra=0, app=False):
        return (card_state.as_value() << 9) | (C(1, 32) << 8) | C(extra, 32) | \
            Mux(app, C(1, 32) << 5, C(0, 32))

    def load_r1(command, status):
        response.load_r1(m, command, status)

    def load_r2(register):
        response.load_r2(m, register)

    def load_r3():
        response.load_r3(m)

    def load_r6(status):
        response.load_r6(m, status)

    stop_requested = control.stop_requested

    response.elaborate(m)
    m.d.comb += control.response_active.eq(response_active)
    m.d.sd += control.card_reset.eq(0)

    with m.If(control.state_update_valid):
        m.d.sd += card_state.eq(control.state_update)
    with m.If(control.mode_update_valid):
        m.d.sd += data_mode.eq(control.mode_update)
    with m.If(control.address_update_valid):
        m.d.sd += current_address.eq(control.address_update)

    with m.If(~enabled_sd):
        m.d.sd += [
            cmd_bits.eq(0),
            cmd_crc_calc.eq(0),
            *response.reset(),
            card_state.eq(CardState.Idle),
            data_mode.eq(DataMode.Idle),
            app_command.eq(0),
        ]
    with m.Elif(~response_active):
        with m.If(cmd_bits == 0):
            with m.If(cmd_i == 0):
                m.d.sd += [
                    cmd_shift.eq(0),
                    cmd_bits.eq(1),
                    cmd_crc_calc.eq(0),
                ]
        with m.Else():
            m.d.sd += [
                cmd_shift.eq(cmd_word),
                cmd_bits.eq(cmd_bits + 1),
            ]
            with m.If(cmd_bits < 40):
                m.d.sd += cmd_crc_calc.eq(_crc7_value(cmd_crc_calc, cmd_i))
            with m.If(cmd_bits == 47):
                m.d.sd += [
                    cmd_bits.eq(0),
                    self.last_command.eq(cmd_index),
                    self.command_count.eq(self.command_count + 1),
                    stop_requested.eq(0),
                ]

                valid_frame = (cmd_word[47] == 0) & (cmd_word[46] == 1) & (cmd_word[0] == 1)
                with m.If(~valid_frame):
                    m.d.sd += [
                        self.protocol_errors.eq(self.protocol_errors + 1),
                        self.protocol_error_sources[0].eq(1),
                    ]
                with m.Elif(cmd_crc != cmd_crc_calc):
                    m.d.sd += [
                        self.protocol_errors.eq(self.protocol_errors + 1),
                        self.protocol_error_sources[1].eq(1),
                    ]
                with m.Else():
                    with m.If(cmd_index != 55):
                        m.d.sd += app_command.eq(0)

                    with m.If(app_command):
                        with m.Switch(cmd_index):
                            with m.Case(13):
                                with m.If(card_state == CardState.Tran):
                                    load_r1(cmd_index, status_value())
                                    m.d.sd += [
                                        data_mode.eq(DataMode.ReadSDStatus),
                                        card_state.eq(CardState.Data),
                                    ]
                                with m.Else():
                                    load_r1(cmd_index, status_value(extra=1 << 22))
                            with m.Case(6):
                                with m.If((card_state == CardState.Tran) &
                                          ((cmd_argument[:2] == 0) |
                                           ((cmd_argument[:2] == 2) &
                                            self.support_4bit))):
                                    load_r1(cmd_index, status_value())
                                    m.d.sd += bus_width_4.eq(cmd_argument[:2] == 2)
                                with m.Else():
                                    load_r1(cmd_index, status_value(extra=1 << 22))
                            with m.Case(23, 42):
                                load_r1(cmd_index, status_value())
                            with m.Case(41):
                                with m.If((card_state == CardState.Idle) |
                                          (card_state == CardState.Ready)):
                                    load_r3()
                                    m.d.sd += card_state.eq(CardState.Ready)
                            with m.Case(51):
                                with m.If(card_state == CardState.Tran):
                                    load_r1(cmd_index, status_value())
                                    m.d.sd += [
                                        data_mode.eq(DataMode.ReadSCR),
                                        card_state.eq(CardState.Data),
                                    ]
                                with m.Else():
                                    load_r1(cmd_index, status_value(extra=1 << 22))
                            with m.Default():
                                load_r1(cmd_index, status_value(extra=1 << 22))
                    with m.Else():
                        with m.Switch(cmd_index):
                            with m.Case(0):
                                m.d.sd += [
                                    *response.reset(),
                                    card_state.eq(CardState.Idle),
                                    data_mode.eq(DataMode.Idle),
                                    bus_width_4.eq(0),
                                    erase_start_valid.eq(0),
                                    erase_end_valid.eq(0),
                                    control.card_reset.eq(1),
                                ]
                            with m.Case(1):
                                # CMD1 is the MMC operating-condition probe. An SD card must not
                                # respond; legacy hosts such as Sharp MAINTE use that timeout to
                                # select the SD ACMD41 initialization path.
                                pass
                            with m.Case(2):
                                with m.If(card_state == CardState.Ready):
                                    load_r2(self.cid)
                                    m.d.sd += card_state.eq(CardState.Ident)
                            with m.Case(3):
                                with m.If((card_state == CardState.Ident) |
                                          (card_state == CardState.Stby)):
                                    load_r6(status_value())
                                    m.d.sd += card_state.eq(CardState.Stby)
                            with m.Case(7):
                                with m.If(cmd_argument[16:32] == RCA):
                                    load_r1(cmd_index, status_value())
                                    m.d.sd += card_state.eq(CardState.Tran)
                                with m.Elif(cmd_argument[16:32] == 0):
                                    with m.If(card_state == CardState.Tran):
                                        load_r1(cmd_index, status_value())
                                    m.d.sd += [
                                        card_state.eq(CardState.Stby),
                                        data_mode.eq(DataMode.Idle),
                                    ]
                            with m.Case(9):
                                with m.If((card_state == CardState.Stby) &
                                          (cmd_argument[16:32] == RCA)):
                                    load_r2(self.csd)
                            with m.Case(10):
                                with m.If((card_state == CardState.Stby) &
                                          (cmd_argument[16:32] == RCA)):
                                    load_r2(self.cid)
                            with m.Case(12):
                                with m.If((card_state == CardState.Data) |
                                          (card_state == CardState.Rcv) |
                                          (card_state == CardState.Prg)):
                                    load_r1(cmd_index, status_value())
                                    m.d.sd += [
                                        stop_requested.eq(1),
                                        data_mode.eq(DataMode.Idle),
                                        card_state.eq(CardState.Tran),
                                    ]
                            with m.Case(13):
                                with m.If(cmd_argument[16:32] == RCA):
                                    load_r1(cmd_index, status_value())
                            with m.Case(15):
                                with m.If(cmd_argument[16:32] == RCA):
                                    m.d.sd += [
                                        card_state.eq(CardState.Dis),
                                        data_mode.eq(DataMode.Idle),
                                    ]
                            with m.Case(16):
                                with m.If(card_state == CardState.Tran):
                                    with m.If(cmd_argument == BLOCK_SIZE):
                                        load_r1(cmd_index, status_value())
                                    with m.Else():
                                        load_r1(cmd_index, status_value(extra=1 << 29))
                            with m.Case(17, 18, 24, 25):
                                status = status_value()
                                aligned = cmd_argument[:9] == 0
                                in_range = cmd_argument <= (self.image_size - BLOCK_SIZE)
                                with m.If(card_state != CardState.Tran):
                                    load_r1(cmd_index, status_value(extra=1 << 22))
                                with m.Elif(self.read_only & cmd_index.matches(24, 25)):
                                    load_r1(cmd_index, status_value(extra=1 << 26))
                                with m.Elif(~aligned):
                                    load_r1(cmd_index, status_value(extra=1 << 30))
                                with m.Elif(~in_range):
                                    load_r1(cmd_index, status_value(extra=1 << 31))
                                with m.Else():
                                    load_r1(cmd_index, status)
                                    if capture_cmd17_wire:
                                        with m.If(cmd_index == 17):
                                            m.d.sd += [
                                                self.clock_count.eq(0),
                                                self.command_low_count.eq(0),
                                                cmd17_capture_bits.eq(0),
                                            ]
                                    m.d.sd += current_address.eq(cmd_argument)
                                    with m.Switch(cmd_index):
                                        with m.Case(17):
                                            m.d.sd += data_mode.eq(DataMode.ReadSingle)
                                        with m.Case(18):
                                            m.d.sd += data_mode.eq(DataMode.ReadMulti)
                                        with m.Case(24):
                                            m.d.sd += data_mode.eq(DataMode.WriteSingle)
                                        with m.Case(25):
                                            m.d.sd += data_mode.eq(DataMode.WriteMulti)
                                    with m.If(cmd_index.matches(17, 18)):
                                        m.d.sd += card_state.eq(CardState.Data)
                                    with m.Else():
                                        m.d.sd += card_state.eq(CardState.Rcv)
                            with m.Case(32):
                                with m.If((card_state == CardState.Tran) &
                                          (cmd_argument[:9] == 0) &
                                          (cmd_argument <= self.image_size - BLOCK_SIZE)):
                                    load_r1(cmd_index, status_value())
                                    m.d.sd += [
                                        erase_start.eq(cmd_argument),
                                        erase_start_valid.eq(1),
                                    ]
                                with m.Else():
                                    load_r1(cmd_index, status_value(extra=1 << 30))
                            with m.Case(33):
                                with m.If((card_state == CardState.Tran) &
                                          (cmd_argument[:9] == 0) &
                                          (cmd_argument <= self.image_size - BLOCK_SIZE)):
                                    load_r1(cmd_index, status_value())
                                    m.d.sd += [
                                        erase_end.eq(cmd_argument),
                                        erase_end_valid.eq(1),
                                    ]
                                with m.Else():
                                    load_r1(cmd_index, status_value(extra=1 << 30))
                            with m.Case(38):
                                with m.If(self.read_only):
                                    load_r1(cmd_index, status_value(extra=1 << 26))
                                    m.d.sd += [
                                        erase_start_valid.eq(0),
                                        erase_end_valid.eq(0),
                                    ]
                                with m.Elif((card_state == CardState.Tran) &
                                          erase_start_valid & erase_end_valid &
                                          (erase_start <= erase_end)):
                                    load_r1(cmd_index, status_value())
                                    m.d.sd += [
                                        data_mode.eq(DataMode.Erase),
                                        card_state.eq(CardState.Prg),
                                        erase_start_valid.eq(0),
                                        erase_end_valid.eq(0),
                                    ]
                                with m.Else():
                                    load_r1(cmd_index, status_value(extra=1 << 28))
                            with m.Case(55):
                                load_r1(cmd_index, status_value(app=True))
                                m.d.sd += app_command.eq(1)
                            with m.Default():
                                load_r1(cmd_index, status_value(extra=1 << 22))
