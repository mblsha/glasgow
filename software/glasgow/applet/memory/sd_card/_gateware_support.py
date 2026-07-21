from amaranth import C, Cat, Mux, Value
from amaranth.lib import enum


BLOCK_SIZE = 512
MAX_IMAGE_SIZE = 16 * 1024 * 1024
IMAGE_SIZE_GRANULARITY = 2048

RCA = 0x1234
OCR = 0x80ff_8000


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


def crc7_value(crc: Value, bit: Value) -> Value:
    feedback = bit ^ crc[6]
    return Cat(C(0, 1), crc[:6]) ^ Mux(feedback, C(0x09, 7), C(0, 7))


def crc16_value(crc: Value, bit: Value) -> Value:
    feedback = bit ^ crc[15]
    return Cat(C(0, 1), crc[:15]) ^ Mux(feedback, C(0x1021, 16), C(0, 16))
