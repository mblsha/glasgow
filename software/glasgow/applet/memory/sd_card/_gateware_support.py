from amaranth import C, Cat, Const, Mux, Value
from amaranth.lib import crc, data, enum

from ._protocol import BLOCK_SIZE


MAX_IMAGE_SIZE = 1024 * 1024 * 1024
IMAGE_SIZE_WIDTH = 31
IMAGE_SIZE_GRANULARITY = 2048
BUFFER_BLOCKS = 2
BUFFER_BANK_BITS = (BUFFER_BLOCKS - 1).bit_length()
BUFFER_STATE_WIDTH = BUFFER_BLOCKS * 3

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
    ReadSDStatus = 7
    ReadWrittenBlocks = 8


def crc7_value(crc: Value, bit: Value) -> Value:
    feedback = bit ^ crc[6]
    return Cat(C(0, 1), crc[:6]) ^ Mux(feedback, C(0x09, 7), C(0, 7))


def crc16_value(crc: Value, bit: Value) -> Value:
    feedback = bit ^ crc[15]
    return Cat(C(0, 1), crc[:15]) ^ Mux(feedback, C(0x1021, 16), C(0, 16))


class GatewareFrameSchema:
    """Compile a host :class:`FrameSchema` into a gateware record and byte operations."""

    def __init__(self, schema):
        self.schema = schema
        self.layout = data.StructLayout({
            field.name: field.byte_width * 8 for field in schema.fields
        })

    def bytes(self, view):
        """Return little-endian header byte values sourced from *view*."""
        result = []
        for field, byte_index in self.schema.byte_fields:
            if field.constant is not None:
                value = Const(field.constant, field.byte_width * 8)
            else:
                value = Value.cast(view[field.name])
            result.append(value.word_select(byte_index, 8))
        return result

    def load_byte(self, m, view, offset, octet, *, domain):
        """Generate a byte-indexed assignment into *view* from *octet*."""
        domain_statements = getattr(m.d, domain)
        with m.Switch(offset):
            for index, (field, byte_index) in enumerate(self.schema.byte_fields):
                with m.Case(index):
                    domain_statements += view[field.name].word_select(byte_index, 8).eq(octet)

    def constants_valid(self, view):
        """Return an expression checking every constant field in *view*."""
        valid = C(1)
        for field in self.schema.fields:
            if field.constant is not None:
                valid &= view[field.name] == field.constant
        return valid
