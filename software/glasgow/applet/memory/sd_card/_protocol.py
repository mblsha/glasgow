from dataclasses import dataclass
from enum import IntEnum
import struct

from glasgow.gateware import cobs


__all__ = [
    "BLOCK_SIZE", "FRAME_VERSION", "REQUEST_SCHEMA", "REPLY_SCHEMA",
    "BackendStatus", "FrameError", "FrameField", "FrameSchema", "HostEvent", "HostOperation",
    "HostReply", "HOST_OPERATIONS", "HOST_OPERATION_FOR_EVENT", "HOST_OPERATION_FOR_REPLY",
    "HOST_EVENT_PAYLOAD_LENGTH", "HOST_REPLY_FOR_EVENT", "HOST_REPLY_PAYLOAD_LENGTH",
    "PayloadField", "PayloadSchema", "RequestFrame", "RequestHeader", "ReplyFrame",
    "ReplyHeader", "frame_crc16",
]


BLOCK_SIZE = 512
FRAME_VERSION = 1


class HostEvent(IntEnum):
    Read  = 0x01
    Write = 0x02
    Erase = 0x03


class HostReply(IntEnum):
    ReadData = 0x81
    WriteAck = 0x82
    EraseAck = 0x83


class BackendStatus(IntEnum):
    OK          = 0x00
    Invalid     = 0x01
    ReadOnly    = 0x02
    IOError     = 0x03
    ShortIO     = 0x04


class FrameError(Exception):
    pass


@dataclass(frozen=True)
class PayloadField:
    name: str
    byte_width: int
    raw: bool = False

    def __post_init__(self):
        if self.byte_width < 1:
            raise ValueError("payload fields must contain at least one byte")


class PayloadSchema:
    """Declarative fixed-size operation payload shared by host and gateware metadata."""

    def __init__(self, name, fields=()):
        self.name = name
        self.fields = tuple(fields)
        self.size = sum(field.byte_width for field in self.fields)
        if len({field.name for field in self.fields}) != len(self.fields):
            raise ValueError(f"duplicate field in {name} payload schema")

    def encode(self, **values):
        result = bytearray()
        for field in self.fields:
            value = values[field.name]
            if field.raw:
                value = bytes(value)
                if len(value) != field.byte_width:
                    raise FrameError(
                        f"{self.name} payload field {field.name} is {len(value)} bytes, "
                        f"expected {field.byte_width}")
                result.extend(value)
            else:
                result.extend(int(value).to_bytes(field.byte_width, "little"))
        return bytes(result)

    def decode(self, value):
        if len(value) != self.size:
            raise FrameError(
                f"{self.name} payload is {len(value)} bytes, expected {self.size}")
        result = {}
        offset = 0
        for field in self.fields:
            encoded = value[offset:offset + field.byte_width]
            offset += field.byte_width
            result[field.name] = encoded if field.raw else int.from_bytes(encoded, "little")
        return result


@dataclass(frozen=True)
class HostOperation:
    event: HostEvent
    reply: HostReply
    request_payload: PayloadSchema
    reply_payload: PayloadSchema

    def __post_init__(self):
        if self.request_payload.name != self.event.name.lower():
            raise ValueError("request payload schema name must match its event")

    def invalid_reply_payload(self):
        return bytes([0xff]) * self.reply_payload.size


HOST_OPERATIONS = (
    HostOperation(
        HostEvent.Read, HostReply.ReadData,
        PayloadSchema("read", (
            PayloadField("address", 4), PayloadField("blocks", 1))),
        PayloadSchema("read_data", (PayloadField("data", BLOCK_SIZE, raw=True),))),
    HostOperation(
        HostEvent.Write, HostReply.WriteAck,
        PayloadSchema("write", (
            PayloadField("address", 4), PayloadField("data", BLOCK_SIZE, raw=True))),
        PayloadSchema("write_ack")),
    HostOperation(
        HostEvent.Erase, HostReply.EraseAck,
        PayloadSchema("erase", (PayloadField("start", 4), PayloadField("end", 4))),
        PayloadSchema("erase_ack")),
)

HOST_OPERATION_FOR_EVENT = {operation.event: operation for operation in HOST_OPERATIONS}
HOST_OPERATION_FOR_REPLY = {operation.reply: operation for operation in HOST_OPERATIONS}

# Compatibility views for callers that only need one attribute. The operation table above is the
# sole source of truth; these mappings must never be edited independently.
HOST_EVENT_PAYLOAD_LENGTH = {
    event: operation.request_payload.size
    for event, operation in HOST_OPERATION_FOR_EVENT.items()
}
HOST_REPLY_FOR_EVENT = {
    event: operation.reply
    for event, operation in HOST_OPERATION_FOR_EVENT.items()
}
HOST_REPLY_PAYLOAD_LENGTH = {
    reply: operation.reply_payload.size
    for reply, operation in HOST_OPERATION_FOR_REPLY.items()
}


@dataclass(frozen=True)
class FrameField:
    name: str
    byte_width: int
    enum: type[IntEnum] | None = None
    constant: int | None = None

    def __post_init__(self):
        if self.byte_width < 1:
            raise ValueError("frame fields must contain at least one byte")
        if self.constant is not None and self.constant not in range(1 << (self.byte_width * 8)):
            raise ValueError(f"constant does not fit in field {self.name}")


class FrameSchema:
    """Declarative little-endian frame header shared by Python and gateware."""

    def __init__(self, name, fields):
        self.name = name
        self.fields = tuple(fields)
        self.size = sum(field.byte_width for field in self.fields)
        if len({field.name for field in self.fields}) != len(self.fields):
            raise ValueError(f"duplicate field in {name} frame schema")

    @property
    def byte_fields(self):
        for field in self.fields:
            for byte_index in range(field.byte_width):
                yield field, byte_index

    def field_offset(self, name):
        offset = 0
        for field in self.fields:
            if field.name == name:
                return offset
            offset += field.byte_width
        raise KeyError(name)

    def encode(self, **values):
        result = bytearray()
        for field in self.fields:
            value = field.constant if field.constant is not None else values[field.name]
            result.extend(int(value).to_bytes(field.byte_width, "little"))
        return bytes(result)

    def decode(self, value):
        if len(value) != self.size:
            raise FrameError(
                f"{self.name} header is {len(value)} bytes, expected {self.size}")
        result = {}
        offset = 0
        for field in self.fields:
            decoded = int.from_bytes(value[offset:offset + field.byte_width], "little")
            offset += field.byte_width
            if field.constant is not None:
                if decoded != field.constant:
                    raise FrameError(
                        f"unsupported {self.name} {field.name} {decoded}")
                continue
            if field.enum is not None:
                try:
                    decoded = field.enum(decoded)
                except ValueError as error:
                    raise FrameError(
                        f"unknown {self.name} {field.name} {decoded:#04x}") from error
            result[field.name] = decoded
        return result


REQUEST_SCHEMA = FrameSchema("request", (
    FrameField("version", 1, constant=FRAME_VERSION),
    FrameField("opcode", 1, enum=HostEvent),
    FrameField("generation", 2),
    FrameField("request_id", 2),
    FrameField("payload_length", 2),
))
REPLY_SCHEMA = FrameSchema("reply", (
    FrameField("version", 1, constant=FRAME_VERSION),
    FrameField("opcode", 1, enum=HostReply),
    FrameField("generation", 2),
    FrameField("request_id", 2),
    FrameField("status", 1, enum=BackendStatus),
    FrameField("payload_length", 2),
))


def frame_crc16(value: bytes) -> int:
    crc = 0
    for octet in value:
        for bitno in reversed(range(8)):
            feedback = ((octet >> bitno) & 1) ^ ((crc >> 15) & 1)
            crc = (crc << 1) & 0xffff
            if feedback:
                crc ^= 0x1021
    return crc


def _encode_packet(frame: bytes) -> bytes:
    return cobs.encode(frame + struct.pack("<H", frame_crc16(frame))) + b"\0"


def _decode_packet(packet: bytes) -> bytes:
    if not packet.endswith(b"\0"):
        raise FrameError("COBS packet has no delimiter")
    try:
        frame = cobs.decode(packet[:-1])
    except cobs.DecodeError as error:
        raise FrameError(f"invalid COBS packet: {error}") from error
    if len(frame) < 2:
        raise FrameError("frame is shorter than its CRC")
    value, received_crc = frame[:-2], struct.unpack("<H", frame[-2:])[0]
    expected_crc = frame_crc16(value)
    if received_crc != expected_crc:
        raise FrameError(
            f"frame CRC mismatch (received {received_crc:#06x}, expected {expected_crc:#06x})")
    return value


class RequestHeader:
    """Header shared by every FPGA-to-host block request."""

    size = REQUEST_SCHEMA.size

    def __init__(self, opcode: HostEvent, generation: int, request_id: int,
            payload_length: int = 0):
        self.opcode = HostEvent(opcode)
        self.generation = generation
        self.request_id = request_id
        self.payload_length = payload_length

    def encode(self) -> bytes:
        return REQUEST_SCHEMA.encode(
            opcode=self.opcode, generation=self.generation, request_id=self.request_id,
            payload_length=self.payload_length)

    @classmethod
    def decode(cls, value: bytes):
        return cls(**REQUEST_SCHEMA.decode(value))


class RequestFrame:
    def __init__(self, header: RequestHeader, payload: bytes = b""):
        self.header = header
        self.payload = bytes(payload)
        if header.payload_length != len(self.payload):
            raise FrameError(
                f"request payload is {len(self.payload)} bytes, header says "
                f"{header.payload_length}")

    @classmethod
    def create(cls, opcode: HostEvent, generation: int, request_id: int, payload=b""):
        payload = bytes(payload)
        return cls(RequestHeader(opcode, generation, request_id, len(payload)), payload)

    def encode_packet(self) -> bytes:
        return _encode_packet(self.header.encode() + self.payload)

    @classmethod
    def decode_packet(cls, packet: bytes):
        value = _decode_packet(packet)
        if len(value) < RequestHeader.size:
            raise FrameError("request frame is shorter than its header")
        header = RequestHeader.decode(value[:RequestHeader.size])
        payload = value[RequestHeader.size:]
        return cls(header, payload)


class ReplyHeader:
    """Header shared by every host-to-FPGA block reply."""

    size = REPLY_SCHEMA.size

    def __init__(self, opcode: HostReply, generation: int, request_id: int,
            status: BackendStatus = BackendStatus.OK, payload_length: int = 0):
        self.opcode = HostReply(opcode)
        self.generation = generation
        self.request_id = request_id
        self.status = BackendStatus(status)
        self.payload_length = payload_length

    def encode(self) -> bytes:
        return REPLY_SCHEMA.encode(
            opcode=self.opcode, generation=self.generation, request_id=self.request_id,
            status=self.status, payload_length=self.payload_length)

    @classmethod
    def decode(cls, value: bytes):
        return cls(**REPLY_SCHEMA.decode(value))


class ReplyFrame:
    def __init__(self, header: ReplyHeader, payload: bytes = b""):
        self.header = header
        self.payload = bytes(payload)
        if header.payload_length != len(self.payload):
            raise FrameError(
                f"reply payload is {len(self.payload)} bytes, header says "
                f"{header.payload_length}")

    @classmethod
    def create(cls, opcode: HostReply, generation: int, request_id: int,
            status: BackendStatus = BackendStatus.OK, payload=b""):
        payload = bytes(payload)
        return cls(
            ReplyHeader(opcode, generation, request_id, status, len(payload)), payload)

    def encode_packet(self) -> bytes:
        return _encode_packet(self.header.encode() + self.payload)

    @classmethod
    def decode_packet(cls, packet: bytes):
        value = _decode_packet(packet)
        if len(value) < ReplyHeader.size:
            raise FrameError("reply frame is shorter than its header")
        header = ReplyHeader.decode(value[:ReplyHeader.size])
        payload = value[ReplyHeader.size:]
        return cls(header, payload)
