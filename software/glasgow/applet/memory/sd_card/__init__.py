from collections.abc import Buffer
import logging
import os
import struct

from amaranth import *
from amaranth.lib import cdc, data, enum, io, memory, stream, wiring
from amaranth.lib.wiring import In, Out

from glasgow.abstract import AbstractAssembly, GlasgowPin
from glasgow.applet import GlasgowAppletError, GlasgowAppletV2
from glasgow.gateware.stream import AsyncQueue


__all__ = [
    "SDCardError", "SDCardComponent", "SDCardInterface", "SDCardApplet",
    "crc7", "crc16", "build_cid", "build_csd", "build_scr",
]


BLOCK_SIZE = 512
MAX_IMAGE_SIZE = 16 * 1024 * 1024
IMAGE_SIZE_GRANULARITY = 2048

RCA = 0x1234
OCR = 0x80ff_8000  # ready, 2.7 V through 3.6 V


class SDCardError(GlasgowAppletError):
    pass


class CardState(enum.Enum, shape=4):
    Idle  = 0
    Ready = 1
    Ident = 2
    Stby  = 3
    Tran  = 4
    Data  = 5
    Rcv   = 6
    Prg   = 7
    Dis   = 8


class DataMode(enum.Enum, shape=4):
    Idle        = 0
    ReadSingle  = 1
    ReadMulti   = 2
    WriteSingle = 3
    WriteMulti  = 4
    ReadSCR     = 5
    Erase       = 6


class HostEvent(enum.Enum, shape=8):
    Read  = 0x01
    Write = 0x02
    Erase = 0x03


class HostReply(enum.Enum, shape=8):
    ReadData = 0x81
    WriteAck = 0x82
    EraseAck = 0x83


def _crc7_step(crc: int, bit: int) -> int:
    feedback = bit ^ ((crc >> 6) & 1)
    crc = (crc << 1) & 0x7f
    if feedback:
        crc ^= 0x09
    return crc


def crc7(value: int, width: int) -> int:
    """Return the SD CRC7 of *width* bits, processed most-significant bit first."""
    crc = 0
    for bitno in reversed(range(width)):
        crc = _crc7_step(crc, (value >> bitno) & 1)
    return crc


def _crc16_step(crc: int, bit: int) -> int:
    feedback = bit ^ ((crc >> 15) & 1)
    crc = (crc << 1) & 0xffff
    if feedback:
        crc ^= 0x1021
    return crc


def crc16(data: Buffer) -> int:
    """Return the SD CRC16-CCITT of *data*, processed most-significant bit first."""
    crc = 0
    for octet in data:
        for bitno in reversed(range(8)):
            crc = _crc16_step(crc, (octet >> bitno) & 1)
    return crc


def _set_field(value: int, high: int, low: int, field: int) -> int:
    width = high - low + 1
    assert field in range(1 << width)
    mask = ((1 << width) - 1) << low
    return (value & ~mask) | (field << low)


def _finish_register_crc(value: int) -> int:
    value = _set_field(value, 7, 1, crc7(value >> 8, 120))
    return _set_field(value, 0, 0, 1)


def build_cid(*, serial: int = 0x474c_4153) -> int:
    """Build a deterministic 128-bit CID register for the emulated card."""
    cid = 0
    cid = _set_field(cid, 127, 120, 0x03)
    cid = _set_field(cid, 119, 104, int.from_bytes(b"GL", "big"))
    cid = _set_field(cid, 103, 64, int.from_bytes(b"HOST ", "big"))
    cid = _set_field(cid, 63, 56, 0x10)
    cid = _set_field(cid, 55, 24, serial & 0xffff_ffff)
    cid = _set_field(cid, 19, 8, ((2026 - 2000) << 4) | 7)
    return _finish_register_crc(cid)


def _capacity_fields(size: int) -> tuple[int, int]:
    if size <= 0 or size > MAX_IMAGE_SIZE:
        raise SDCardError(
            f"image size must be between {IMAGE_SIZE_GRANULARITY} bytes and "
            f"{MAX_IMAGE_SIZE} bytes")
    if size % IMAGE_SIZE_GRANULARITY:
        raise SDCardError(
            f"image size must be a multiple of {IMAGE_SIZE_GRANULARITY} bytes")

    block_count = size // BLOCK_SIZE
    for c_size_mult in reversed(range(8)):
        multiplier = 1 << (c_size_mult + 2)
        if block_count % multiplier == 0:
            c_size = block_count // multiplier - 1
            if c_size in range(1 << 12):
                return c_size, c_size_mult
    raise SDCardError(f"image size {size} cannot be represented by a CSD v1 register")


def build_csd(size: int) -> int:
    """Build a CSD v1 register describing a byte-addressed SDSC image."""
    c_size, c_size_mult = _capacity_fields(size)

    csd = 0
    csd = _set_field(csd, 127, 126, 0)       # CSD_STRUCTURE v1
    csd = _set_field(csd, 119, 112, 0x7f)    # TAAC: 80 ms
    csd = _set_field(csd, 111, 104, 0xff)    # NSAC: 25,500 clocks
    csd = _set_field(csd, 103, 96, 0x32)     # TRAN_SPEED: 25 MHz
    csd = _set_field(csd, 95, 84, 0x135)     # classes 0, 2, 4, 5, 8
    csd = _set_field(csd, 83, 80, 9)         # READ_BL_LEN: 512 bytes
    csd = _set_field(csd, 79, 79, 0)         # fixed-size reads only
    csd = _set_field(csd, 78, 78, 0)         # no write misalignment
    csd = _set_field(csd, 77, 77, 0)         # no read misalignment
    csd = _set_field(csd, 76, 76, 0)         # no DSR
    csd = _set_field(csd, 73, 62, c_size)
    csd = _set_field(csd, 49, 47, c_size_mult)
    csd = _set_field(csd, 46, 46, 1)         # single-block erase
    csd = _set_field(csd, 45, 39, 0)         # one write block per erase sector
    csd = _set_field(csd, 31, 31, 0)         # no write-protect groups
    csd = _set_field(csd, 28, 26, 7)         # conservative write-time factor
    csd = _set_field(csd, 25, 22, 9)         # WRITE_BL_LEN: 512 bytes
    csd = _set_field(csd, 21, 21, 0)         # fixed-size writes only
    csd = _set_field(csd, 15, 15, 0)
    csd = _set_field(csd, 11, 10, 0)         # partitioned/DOS-style image
    return _finish_register_crc(csd)


def build_scr() -> int:
    """Build an SD 1.01 SCR supporting both 1-bit and 4-bit buses."""
    scr = 0
    scr = _set_field(scr, 63, 60, 0)         # SCR_STRUCTURE v1
    scr = _set_field(scr, 59, 56, 0)         # SD_SPEC 1.0/1.01
    scr = _set_field(scr, 55, 55, 1)         # erased data reads as ones
    scr = _set_field(scr, 54, 52, 0)         # no security protocol
    scr = _set_field(scr, 51, 48, 0b0101)    # 1-bit and 4-bit buses
    return scr


def _crc7_value(crc: Value, bit: Value) -> Value:
    feedback = bit ^ crc[6]
    return Cat(C(0, 1), crc[:6]) ^ Mux(feedback, C(0x09, 7), C(0, 7))


def _crc16_value(crc: Value, bit: Value) -> Value:
    feedback = bit ^ crc[15]
    return Cat(C(0, 1), crc[:15]) ^ Mux(feedback, C(0x1021, 16), C(0, 16))


class SDCardComponent(wiring.Component):
    i_stream: In(stream.Signature(8))
    o_stream: Out(stream.Signature(8))
    o_flush:  Out(1)

    enabled:        In(1)
    image_size:     In(25)
    cid:            In(128)
    csd:            In(128)
    scr:            In(64)

    last_command:   Out(6)
    clock_count:    Out(32)
    command_low_count: Out(32)
    command_count:  Out(32)
    read_count:     Out(32)
    write_count:    Out(32)
    protocol_errors: Out(16)

    def __init__(self, ports):
        self._ports = ports
        super().__init__()

    def elaborate(self, platform):
        m = Module()

        m.submodules.clk_buffer = clk_buffer = io.Buffer("i", self._ports.clk)
        m.submodules.cmd_buffer = cmd_buffer = io.Buffer("io", self._ports.cmd)
        dat_buffers = []
        for index, dat_port in enumerate(self._ports.dat):
            buffer = io.Buffer("io", dat_port)
            m.submodules[f"dat_buffer_{index}"] = buffer
            dat_buffers.append(buffer)

        if platform is not None:
            platform.add_clock_constraint(clk_buffer.i, 25e6)

        m.domains.sd = cd_sd = ClockDomain(async_reset=True, local=True)
        m.domains.sd_fall = cd_sd_fall = ClockDomain(async_reset=True, local=True)
        m.d.comb += [
            cd_sd.clk.eq(clk_buffer.i),
            cd_sd.rst.eq(ResetSignal() | ~self.enabled),
            cd_sd_fall.clk.eq(~clk_buffer.i),
            cd_sd_fall.rst.eq(ResetSignal() | ~self.enabled),
        ]

        enabled_sd = Signal()
        m.submodules.enabled_sync = cdc.FFSynchronizer(self.enabled, enabled_sd, o_domain="sd")

        host_to_sd = AsyncQueue(shape=8, depth=1024, i_domain="sync", o_domain="sd")
        sd_to_host = AsyncQueue(
            shape=data.StructLayout({"data": 8, "end": 1}),
            depth=1024, i_domain="sd", o_domain="sync")
        m.submodules.host_to_sd = host_to_sd
        m.submodules.sd_to_host = sd_to_host

        wiring.connect(m, host_to_sd.i, wiring.flipped(self.i_stream))
        m.d.comb += [
            self.o_stream.payload.eq(sd_to_host.o.p.data),
            self.o_stream.valid.eq(sd_to_host.o.valid),
            sd_to_host.o.ready.eq(self.o_stream.ready),
        ]
        m.d.sync += self.o_flush.eq(0)
        with m.If(self.o_stream.valid & self.o_stream.ready & sd_to_host.o.p.end):
            m.d.sync += self.o_flush.eq(1)

        block_memory = memory.Memory(shape=8, depth=BLOCK_SIZE, init=[])
        m.submodules.block_memory = block_memory
        block_write = block_memory.write_port(domain="sd")
        block_read = block_memory.read_port(domain="sd")

        card_state = Signal(CardState, init=CardState.Idle)
        data_mode = Signal(DataMode, init=DataMode.Idle)
        bus_width_4 = Signal()
        current_address = Signal(32)
        erase_start = Signal(32)
        erase_end = Signal(32)
        erase_start_valid = Signal()
        erase_end_valid = Signal()
        app_command = Signal()

        cmd_shift = Signal(48)
        cmd_bits = Signal(range(49))
        cmd_crc_calc = Signal(7)
        response_shift = Signal(136)
        response_bits = Signal(range(137))
        response_has_crc = Signal()
        response_crc = Signal(7)
        response_crc_bits = Signal(range(8))
        response_end = Signal()
        response_pending = Signal(2)
        response_pending_prefix = Signal(40)
        response_pending_data = Signal(136)
        response_driving = (response_bits != 0) | (response_crc_bits != 0) | response_end
        response_active = response_driving | (response_pending != 0)

        cmd_word = Cat(cmd_buffer.i, cmd_shift[:47])
        cmd_index = cmd_word[40:46]
        cmd_argument = cmd_word[8:40]
        cmd_crc = cmd_word[1:8]

        # These counters are deliberately kept in the SD clock domain. Besides being useful
        # diagnostics on a real host, they make it possible to distinguish a wiring or clocking
        # problem from a malformed command frame without buffering a full logic trace.
        with m.If(enabled_sd):
            m.d.sd += self.clock_count.eq(self.clock_count + 1)
            with m.If(~cmd_buffer.i):
                m.d.sd += self.command_low_count.eq(self.command_low_count + 1)

        cmd_o_next = Mux(
            response_bits != 0, response_shift[135],
            Mux(response_crc_bits != 0, response_crc[6], 1))
        cmd_o = Signal(init=1)
        cmd_oe = Signal()
        m.d.sd_fall += [
            cmd_o.eq(cmd_o_next),
            cmd_oe.eq(response_driving & enabled_sd),
        ]
        m.d.comb += [
            cmd_buffer.o.eq(cmd_o),
            cmd_buffer.oe.eq(cmd_oe),
        ]

        dat_i = Cat(*(buffer.i for buffer in dat_buffers))
        dat_o = Signal(4, init=0xf)
        dat_oe = Signal(4)
        dat_o_latched = Signal(4, init=0xf)
        dat_oe_latched = Signal(4)
        m.d.sd_fall += [
            dat_o_latched.eq(dat_o),
            dat_oe_latched.eq(Mux(enabled_sd, dat_oe, 0)),
        ]
        for index, buffer in enumerate(dat_buffers):
            m.d.comb += [
                buffer.o.eq(dat_o_latched[index]),
                buffer.oe.eq(dat_oe_latched[index]),
            ]

        def status_value(*, extra=0, app=False):
            return (card_state.as_value() << 9) | (C(1, 32) << 8) | C(extra, 32) | \
                Mux(app, C(1, 32) << 5, C(0, 32))

        def load_r1(command, status):
            m.d.sd += [
                response_pending.eq(1),
                response_pending_prefix[:32].eq(status),
                response_pending_prefix[32:38].eq(command),
                response_pending_prefix[38:40].eq(0),
            ]

        def load_r2(register):
            response = Cat(C(1, 1), register[1:128], C(0x3f, 6), C(0, 2))
            m.d.sd += [
                response_pending.eq(3),
                response_pending_data.eq(response),
            ]

        def load_r3():
            response = Cat(C(1, 1), C(0x7f, 7), C(OCR, 32), C(0x3f, 6), C(0, 2))
            m.d.sd += [
                response_pending.eq(2),
                response_pending_data.eq(response),
            ]

        def load_r6(status):
            r6_status = Cat(status[:13], status[19], status[22], status[23])
            argument = Cat(r6_status, C(RCA, 16))
            m.d.sd += [
                response_pending.eq(1),
                response_pending_prefix[:32].eq(argument),
                response_pending_prefix[32:38].eq(3),
                response_pending_prefix[38:40].eq(0),
            ]

        read_request = Signal()
        write_request = Signal()
        erase_request = Signal()
        stop_requested = Signal()
        request_sent = Signal()

        with m.If(response_bits != 0):
            m.d.sd += [
                response_shift.eq(response_shift << 1),
                response_bits.eq(response_bits - 1),
            ]
            with m.If(response_has_crc):
                m.d.sd += response_crc.eq(_crc7_value(response_crc, response_shift[135]))
                with m.If(response_bits == 1):
                    m.d.sd += response_crc_bits.eq(7)
        with m.Elif(response_crc_bits != 0):
            m.d.sd += [
                response_crc.eq(response_crc << 1),
                response_crc_bits.eq(response_crc_bits - 1),
            ]
            with m.If(response_crc_bits == 1):
                m.d.sd += response_end.eq(1)
        with m.Elif(response_end):
            m.d.sd += [
                response_end.eq(0),
                response_has_crc.eq(0),
            ]
        with m.Elif(response_pending != 0):
            with m.Switch(response_pending):
                with m.Case(1):
                    m.d.sd += [
                        response_shift.eq(response_pending_prefix << 96),
                        response_bits.eq(40),
                        response_has_crc.eq(1),
                        response_crc.eq(0),
                    ]
                with m.Case(2):
                    m.d.sd += [
                        response_shift.eq(response_pending_data << 88),
                        response_bits.eq(48),
                        response_has_crc.eq(0),
                    ]
                with m.Case(3):
                    m.d.sd += [
                        response_shift.eq(response_pending_data),
                        response_bits.eq(136),
                        response_has_crc.eq(0),
                    ]
            m.d.sd += response_pending.eq(0)

        with m.If(~enabled_sd):
            m.d.sd += [
                cmd_bits.eq(0),
                cmd_crc_calc.eq(0),
                response_bits.eq(0),
                response_crc_bits.eq(0),
                response_end.eq(0),
                response_pending.eq(0),
                card_state.eq(CardState.Idle),
                data_mode.eq(DataMode.Idle),
                app_command.eq(0),
            ]
        with m.Elif(~response_active):
            with m.If(cmd_bits == 0):
                with m.If(cmd_buffer.i == 0):
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
                    m.d.sd += cmd_crc_calc.eq(_crc7_value(cmd_crc_calc, cmd_buffer.i))
                with m.If(cmd_bits == 47):
                    m.d.sd += [
                        cmd_bits.eq(0),
                        self.last_command.eq(cmd_index),
                        self.command_count.eq(self.command_count + 1),
                        read_request.eq(0),
                        write_request.eq(0),
                        erase_request.eq(0),
                        stop_requested.eq(0),
                    ]

                    valid_frame = (cmd_word[47] == 0) & (cmd_word[46] == 1) & (cmd_word[0] == 1)
                    with m.If(~valid_frame | (cmd_crc != cmd_crc_calc)):
                        m.d.sd += self.protocol_errors.eq(self.protocol_errors + 1)
                    with m.Else():
                        with m.If(cmd_index != 55):
                            m.d.sd += app_command.eq(0)

                        with m.If(app_command):
                            with m.Switch(cmd_index):
                                with m.Case(6):
                                    with m.If((card_state == CardState.Tran) &
                                              ((cmd_argument[:2] == 0) |
                                               (cmd_argument[:2] == 2))):
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
                                        response_bits.eq(0),
                                        response_crc_bits.eq(0),
                                        response_end.eq(0),
                                        response_pending.eq(0),
                                        card_state.eq(CardState.Idle),
                                        data_mode.eq(DataMode.Idle),
                                        bus_width_4.eq(0),
                                        erase_start_valid.eq(0),
                                        erase_end_valid.eq(0),
                                    ]
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
                                    with m.Elif(~aligned):
                                        load_r1(cmd_index, status_value(extra=1 << 30))
                                    with m.Elif(~in_range):
                                        load_r1(cmd_index, status_value(extra=1 << 31))
                                    with m.Else():
                                        load_r1(cmd_index, status)
                                        m.d.sd += current_address.eq(cmd_argument)
                                        with m.Switch(cmd_index):
                                            with m.Case(17):
                                                m.d.sd += [
                                                    data_mode.eq(DataMode.ReadSingle),
                                                    request_sent.eq(0),
                                                ]
                                            with m.Case(18):
                                                m.d.sd += [
                                                    data_mode.eq(DataMode.ReadMulti),
                                                    request_sent.eq(0),
                                                ]
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
                                    with m.If((card_state == CardState.Tran) &
                                              erase_start_valid & erase_end_valid &
                                              (erase_start <= erase_end)):
                                        load_r1(cmd_index, status_value())
                                        m.d.sd += [
                                            data_mode.eq(DataMode.Erase),
                                            card_state.eq(CardState.Prg),
                                            erase_request.eq(1),
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

        # Host reply receiver. A complete read response is staged into one 512-byte block RAM.
        read_buffer_ready = Signal()
        read_buffer_address = Signal(32)
        read_buffer_consume = Signal()
        read_in_flight = Signal()
        read_in_flight_address = Signal(32)
        host_ack_valid = Signal()
        host_ack_status = Signal(8)
        host_ack_consume = Signal()
        reply_count = Signal(range(BLOCK_SIZE + 1))

        with m.If(read_buffer_consume):
            m.d.sd += read_buffer_ready.eq(0)
        with m.If(host_ack_consume):
            m.d.sd += host_ack_valid.eq(0)

        m.d.comb += [
            host_to_sd.o.ready.eq(0),
            block_write.en.eq(0),
            block_write.addr.eq(0),
            block_write.data.eq(0),
        ]
        with m.FSM(domain="sd", name="host_reply"):
            with m.State("Opcode"):
                m.d.comb += host_to_sd.o.ready.eq(1)
                with m.If(host_to_sd.o.valid):
                    with m.Switch(host_to_sd.o.payload):
                        with m.Case(HostReply.ReadData):
                            m.d.sd += reply_count.eq(0)
                            m.next = "Read-Data"
                        with m.Case(HostReply.WriteAck, HostReply.EraseAck):
                            m.next = "Ack"
                        with m.Default():
                            m.d.sd += self.protocol_errors.eq(self.protocol_errors + 1)

            with m.State("Read-Data"):
                m.d.comb += [
                    host_to_sd.o.ready.eq(1),
                    block_write.en.eq(host_to_sd.o.valid),
                    block_write.addr.eq(reply_count),
                    block_write.data.eq(host_to_sd.o.payload),
                ]
                with m.If(host_to_sd.o.valid):
                    with m.If(reply_count == BLOCK_SIZE - 1):
                        m.d.sd += [
                            read_buffer_ready.eq(1),
                            read_buffer_address.eq(read_in_flight_address),
                            read_in_flight.eq(0),
                        ]
                        m.next = "Opcode"
                    with m.Else():
                        m.d.sd += reply_count.eq(reply_count + 1)

            with m.State("Ack"):
                m.d.comb += host_to_sd.o.ready.eq(1)
                with m.If(host_to_sd.o.valid):
                    m.d.sd += [
                        host_ack_status.eq(host_to_sd.o.payload),
                        host_ack_valid.eq(1),
                    ]
                    m.next = "Opcode"

        # Event transmitter. Events are framed by the `end` bit so the USB side flushes short
        # requests immediately instead of waiting for a full endpoint packet.
        event_address = Signal(32)
        event_count = Signal(range(BLOCK_SIZE + 1))
        event_read_address = Signal(range(BLOCK_SIZE))
        event_read_advance = Signal()
        event_kind = Signal(HostEvent, init=HostEvent.Read)
        event_pending = Signal()

        with m.If(read_request & ~event_pending & ~read_in_flight):
            m.d.sd += [
                event_kind.eq(HostEvent.Read),
                event_address.eq(current_address),
                read_in_flight_address.eq(current_address),
                event_pending.eq(1),
                read_request.eq(0),
            ]
        with m.Elif(write_request & ~event_pending):
            m.d.sd += [
                event_kind.eq(HostEvent.Write),
                event_address.eq(current_address),
                event_pending.eq(1),
                write_request.eq(0),
            ]
        with m.Elif(erase_request & ~event_pending):
            m.d.sd += [
                event_kind.eq(HostEvent.Erase),
                event_address.eq(erase_start),
                event_pending.eq(1),
                erase_request.eq(0),
            ]

        m.d.comb += [
            sd_to_host.i.valid.eq(0),
            sd_to_host.i.p.data.eq(0),
            sd_to_host.i.p.end.eq(0),
            event_read_advance.eq(0),
        ]

        with m.FSM(domain="sd", name="host_event"):
            with m.State("Idle"):
                with m.If(event_pending):
                    m.d.sd += event_count.eq(0)
                    m.next = "Opcode"

            with m.State("Opcode"):
                m.d.comb += [
                    sd_to_host.i.valid.eq(1),
                    sd_to_host.i.p.data.eq(event_kind),
                ]
                with m.If(sd_to_host.i.ready):
                    m.next = "Address"

            with m.State("Address"):
                m.d.comb += [
                    sd_to_host.i.valid.eq(1),
                    sd_to_host.i.p.data.eq(event_address.word_select(event_count, 8)),
                ]
                with m.If(sd_to_host.i.ready):
                    with m.If(event_count == 3):
                        m.d.sd += event_count.eq(0)
                        with m.Switch(event_kind):
                            with m.Case(HostEvent.Read):
                                m.d.comb += sd_to_host.i.p.end.eq(1)
                                m.d.sd += [
                                    event_pending.eq(0),
                                    read_in_flight.eq(1),
                                ]
                                m.next = "Idle"
                            with m.Case(HostEvent.Write):
                                m.d.sd += event_read_address.eq(0)
                                m.next = "Write-Prefetch"
                            with m.Case(HostEvent.Erase):
                                m.next = "Erase-End"
                    with m.Else():
                        m.d.sd += event_count.eq(event_count + 1)

            with m.State("Write-Prefetch"):
                m.next = "Write-Data"

            with m.State("Write-Data"):
                m.d.comb += [
                    sd_to_host.i.valid.eq(1),
                    sd_to_host.i.p.data.eq(block_read.data),
                ]
                with m.If(sd_to_host.i.ready):
                    with m.If(event_count == BLOCK_SIZE - 1):
                        m.d.comb += sd_to_host.i.p.end.eq(1)
                        m.d.sd += event_pending.eq(0)
                        m.next = "Idle"
                    with m.Else():
                        m.d.comb += event_read_advance.eq(1)
                        m.d.sd += [
                            event_count.eq(event_count + 1),
                            event_read_address.eq(event_count + 1),
                        ]

            with m.State("Erase-End"):
                m.d.comb += [
                    sd_to_host.i.valid.eq(1),
                    sd_to_host.i.p.data.eq(erase_end.word_select(event_count, 8)),
                ]
                with m.If(sd_to_host.i.ready):
                    with m.If(event_count == 3):
                        m.d.comb += sd_to_host.i.p.end.eq(1)
                        m.d.sd += event_pending.eq(0)
                        m.next = "Idle"
                    with m.Else():
                        m.d.sd += event_count.eq(event_count + 1)

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

        # The single block RAM read port is shared by card-to-host writes and host-to-card reads;
        # those operations are mutually exclusive by construction.
        with m.If(event_pending & (event_kind == HostEvent.Write)):
            m.d.comb += block_read.addr.eq(
                Mux(event_read_advance, event_count + 1, event_read_address))
        with m.Else():
            m.d.comb += block_read.addr.eq(Mux(read_advance, data_byte + 1, data_byte))

        m.d.comb += [
            dat_o.eq(0xf),
            dat_oe.eq(0),
            read_buffer_consume.eq(0),
            host_ack_consume.eq(0),
            read_advance.eq(0),
        ]

        with m.FSM(domain="sd", name="data_transfer"):
            with m.State("Idle"):
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
                    block_read.data)
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
                        m.d.sd += [
                            current_address.eq(current_address + BLOCK_SIZE),
                            request_sent.eq(0),
                        ]
                    with m.Else():
                        m.d.sd += [
                            data_mode.eq(DataMode.Idle),
                            card_state.eq(CardState.Tran),
                            self.protocol_errors.eq(self.protocol_errors + 1),
                        ]
                with m.Else():
                    m.d.sd += [
                        data_mode.eq(DataMode.Idle),
                        card_state.eq(CardState.Tran),
                    ]
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
                            block_write.en.eq(1),
                            block_write.addr.eq(data_byte),
                            block_write.data.eq(Cat(dat_i, high_nibble)),
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
                            block_write.en.eq(1),
                            block_write.addr.eq(data_byte),
                            block_write.data.eq(next_octet),
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
                        m.d.sd += [
                            write_request.eq(1),
                            card_state.eq(CardState.Prg),
                        ]
                        m.next = "Busy"
                    with m.Else():
                        m.d.sd += [
                            data_mode.eq(DataMode.Idle),
                            card_state.eq(CardState.Tran),
                        ]
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
                            m.d.sd += [
                                current_address.eq(current_address + BLOCK_SIZE),
                                card_state.eq(CardState.Rcv),
                            ]
                            m.next = "Write-Wait-Start"
                        with m.Else():
                            m.d.sd += [
                                data_mode.eq(DataMode.Idle),
                                card_state.eq(CardState.Tran),
                                self.protocol_errors.eq(self.protocol_errors + 1),
                            ]
                            m.next = "Idle"
                    with m.Else():
                        with m.If((host_ack_status == 0) &
                                  (data_mode == DataMode.WriteSingle)):
                            m.d.sd += self.write_count.eq(self.write_count + 1)
                        with m.If(host_ack_status != 0):
                            m.d.sd += self.protocol_errors.eq(self.protocol_errors + 1)
                        m.d.sd += [
                            data_mode.eq(DataMode.Idle),
                            card_state.eq(CardState.Tran),
                        ]
                        m.next = "Idle"

        # Disabling the applet always releases the shared card bus, even if it happens halfway
        # through a transfer. The command engine clears its state on the following SD clock.
        with m.If(~enabled_sd):
            m.d.comb += dat_oe.eq(0)

        return m


class SDCardInterface:
    def __init__(self, logger: logging.Logger, assembly: AbstractAssembly, *,
            clk: GlasgowPin, cmd: GlasgowPin, dat: tuple[GlasgowPin, ...]):
        self._logger = logger
        self._level = logging.DEBUG if self._logger.name == __name__ else logging.TRACE

        ports = assembly.add_port_group(clk=clk, cmd=cmd, dat=dat)
        assembly.use_pulls({cmd: "high", dat: "high"})
        component = assembly.add_submodule(SDCardComponent(ports))
        self._pipe = assembly.add_inout_pipe(
            component.o_stream, component.i_stream, in_flush=component.o_flush,
            in_fifo_depth=0, out_fifo_depth=0,
            in_buffer_size=4096, out_buffer_size=4096)

        self._enabled = assembly.add_rw_register(component.enabled)
        self._image_size = assembly.add_rw_register(component.image_size)
        self._cid = assembly.add_rw_register(component.cid)
        self._csd = assembly.add_rw_register(component.csd)
        self._scr = assembly.add_rw_register(component.scr)
        self.last_command = assembly.add_ro_register(component.last_command)
        self.clock_count = assembly.add_ro_register(component.clock_count)
        self.command_low_count = assembly.add_ro_register(component.command_low_count)
        self.command_count = assembly.add_ro_register(component.command_count)
        self.read_count = assembly.add_ro_register(component.read_count)
        self.write_count = assembly.add_ro_register(component.write_count)
        self.protocol_errors = assembly.add_ro_register(component.protocol_errors)

        self._fd = None
        self._size = None

    async def configure(self, image_path: str):
        fd = os.open(image_path, os.O_RDWR)
        try:
            stat = os.fstat(fd)
            size = stat.st_size
            csd = build_csd(size)
            serial = (stat.st_ino ^ stat.st_size) & 0xffff_ffff
            cid = build_cid(serial=serial)
            scr = build_scr()

            await self._enabled.set(0)
            await self._image_size.set(size)
            await self._cid.set(cid)
            await self._csd.set(csd)
            await self._scr.set(scr)
            await self._enabled.set(1)
        except BaseException:
            os.close(fd)
            raise

        if self._fd is not None:
            os.close(self._fd)
        self._fd = fd
        self._size = size
        self._logger.info("serving %s as a %.2f MiB SDSC image", image_path, size / 2**20)

    async def close(self):
        statistics = None
        if self._fd is not None:
            statistics = (
                await self.clock_count, await self.command_low_count,
                await self.command_count, await self.read_count,
                await self.write_count, await self.protocol_errors,
            )
        await self._enabled.set(0)
        if statistics is not None:
            self._logger.info(
                "stopped after %d SD clocks (%d CMD-low), %d commands, %d reads, %d writes, "
                "%d protocol errors",
                *statistics)
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
            self._size = None

    async def _reply(self, opcode: HostReply, payload=b""):
        await self._pipe.send(bytes([opcode.value]) + bytes(payload))
        await self._pipe.flush()

    async def serve_once(self):
        if self._fd is None or self._size is None:
            raise SDCardError("no image is configured")

        opcode, = await self._pipe.recv(1)
        if opcode == HostEvent.Read.value:
            address, = struct.unpack("<I", await self._pipe.recv(4))
            self._logger.log(self._level, "read block at %#x", address)
            if address % BLOCK_SIZE or address > self._size - BLOCK_SIZE:
                self._logger.error("read request outside image: %#x", address)
                data = bytes([0xff]) * BLOCK_SIZE
            else:
                data = os.pread(self._fd, BLOCK_SIZE, address)
                if len(data) != BLOCK_SIZE:
                    raise SDCardError(f"short image read at {address:#x}")
            await self._reply(HostReply.ReadData, data)

        elif opcode == HostEvent.Write.value:
            address, = struct.unpack("<I", await self._pipe.recv(4))
            data = await self._pipe.recv(BLOCK_SIZE)
            self._logger.log(self._level, "write block at %#x", address)
            status = 0
            try:
                if address % BLOCK_SIZE or address > self._size - BLOCK_SIZE:
                    raise SDCardError(f"write request outside image: {address:#x}")
                if os.pwrite(self._fd, data, address) != BLOCK_SIZE:
                    raise SDCardError(f"short image write at {address:#x}")
            except (OSError, SDCardError) as error:
                self._logger.error("host-backed SD write failed: %s", error)
                status = 1
            await self._reply(HostReply.WriteAck, bytes([status]))

        elif opcode == HostEvent.Erase.value:
            start, end = struct.unpack("<II", await self._pipe.recv(8))
            self._logger.log(self._level, "erase blocks at %#x..%#x", start, end)
            status = 0
            try:
                if start % BLOCK_SIZE or end % BLOCK_SIZE or start > end:
                    raise SDCardError(f"invalid erase range {start:#x}..{end:#x}")
                if end > self._size - BLOCK_SIZE:
                    raise SDCardError(f"erase request outside image: {start:#x}..{end:#x}")
                erased = bytes([0xff]) * BLOCK_SIZE
                for address in range(start, end + 1, BLOCK_SIZE):
                    if os.pwrite(self._fd, erased, address) != BLOCK_SIZE:
                        raise SDCardError(f"short image erase write at {address:#x}")
            except (OSError, SDCardError) as error:
                self._logger.error("host-backed SD erase failed: %s", error)
                status = 1
            await self._reply(HostReply.EraseAck, bytes([status]))

        else:
            raise SDCardError(f"unknown FPGA event opcode {opcode:#04x}")

    async def serve(self):
        try:
            while True:
                await self.serve_once()
        finally:
            await self.close()


class SDCardApplet(GlasgowAppletV2):
    logger = logging.getLogger(__name__)
    help = "emulate a host-backed SD memory card"
    description = """
    Emulate a byte-addressed SDSC memory card using a disk image on the host.

    The FPGA stores only one 512-byte data block and bounded transport FIFOs. Read blocks are
    fetched from the host during the card access delay, and write blocks keep DAT0 busy until the
    host has written them to the image. Images must be 2 KiB aligned and no larger than 16 MiB.

    The default pins match the SD-to-Glasgow breakout on port A. For port B, use ``--clk B2
    --cmd B5 --dat B1,B0,B7,B6``. The DAT pins are specified in DAT0,DAT1,DAT2,DAT3 order.
    """
    required_revision = "C0"
    preview = True

    @classmethod
    def add_build_arguments(cls, parser, access):
        access.add_voltage_argument(parser)
        access.add_pins_argument(parser, "clk", default="A2", required=True)
        access.add_pins_argument(parser, "cmd", default="A5", required=True)
        access.add_pins_argument(
            parser, "dat", width=4, default="A1,A0,A7,A6", required=True,
            help="bind DAT0,DAT1,DAT2,DAT3 in that order")

    def build(self, args):
        with self.assembly.add_applet(self):
            self.assembly.use_voltage(args.voltage)
            self.sd_card_iface = SDCardInterface(self.logger, self.assembly,
                clk=args.clk, cmd=args.cmd, dat=args.dat)

    @classmethod
    def add_setup_arguments(cls, parser):
        parser.add_argument("image", metavar="IMAGE",
            help="serve IMAGE as a writable SDSC card (2 KiB aligned, at most 16 MiB)")

    async def setup(self, args):
        try:
            await self.sd_card_iface.configure(args.image)
        except OSError as error:
            raise SDCardError(f"cannot open image {args.image!r}: {error.strerror}") from error

    async def run(self, args):
        await self.sd_card_iface.serve()

    @classmethod
    def tests(cls):
        from . import test
        return test.SDCardAppletTestCase
