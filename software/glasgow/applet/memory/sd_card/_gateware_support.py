from amaranth import C, Cat, Const, Mux, Value
from amaranth.lib import crc, enum

from ._protocol import BLOCK_SIZE


MAX_IMAGE_SIZE = 16 * 1024 * 1024
IMAGE_SIZE_GRANULARITY = 2048

RCA = 0x1234
OCR = 0x80ff_8000

FRAME_CRC16 = crc.Algorithm(
    crc_width=16, polynomial=0x1021, initial_crc=0,
    reflect_input=False, reflect_output=False, xor_output=0)


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


def frame_header_bytes(schema, values):
    """Return gateware byte values for a declarative little-endian frame schema."""
    result = []
    for field, byte_index in schema.byte_fields:
        if field.constant is not None:
            value = Const(field.constant, field.byte_width * 8)
        else:
            value = Value.cast(values[field.name])
        result.append(value.word_select(byte_index, 8))
    return result
