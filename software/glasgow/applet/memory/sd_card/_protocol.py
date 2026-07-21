from enum import IntEnum
import struct

from glasgow.gateware import cobs


__all__ = [
    "FRAME_VERSION", "BackendStatus", "FrameError", "HostEvent", "HostReply",
    "RequestFrame", "RequestHeader", "ReplyFrame", "ReplyHeader", "frame_crc16",
]


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

    _STRUCT = struct.Struct("<BBHHH")
    size = _STRUCT.size

    def __init__(self, opcode: HostEvent, generation: int, request_id: int,
            payload_length: int = 0):
        self.opcode = HostEvent(opcode)
        self.generation = generation
        self.request_id = request_id
        self.payload_length = payload_length

    def encode(self) -> bytes:
        return self._STRUCT.pack(
            FRAME_VERSION, self.opcode, self.generation, self.request_id,
            self.payload_length)

    @classmethod
    def decode(cls, value: bytes):
        version, opcode, generation, request_id, payload_length = cls._STRUCT.unpack(value)
        if version != FRAME_VERSION:
            raise FrameError(f"unsupported request frame version {version}")
        try:
            return cls(HostEvent(opcode), generation, request_id, payload_length)
        except ValueError as error:
            raise FrameError(f"unknown request opcode {opcode:#04x}") from error


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

    _STRUCT = struct.Struct("<BBHHBH")
    size = _STRUCT.size

    def __init__(self, opcode: HostReply, generation: int, request_id: int,
            status: BackendStatus = BackendStatus.OK, payload_length: int = 0):
        self.opcode = HostReply(opcode)
        self.generation = generation
        self.request_id = request_id
        self.status = BackendStatus(status)
        self.payload_length = payload_length

    def encode(self) -> bytes:
        return self._STRUCT.pack(
            FRAME_VERSION, self.opcode, self.generation, self.request_id, self.status,
            self.payload_length)

    @classmethod
    def decode(cls, value: bytes):
        version, opcode, generation, request_id, status, payload_length = \
            cls._STRUCT.unpack(value)
        if version != FRAME_VERSION:
            raise FrameError(f"unsupported reply frame version {version}")
        try:
            return cls(
                HostReply(opcode), generation, request_id, BackendStatus(status), payload_length)
        except ValueError as error:
            raise FrameError(
                f"unknown reply opcode/status {opcode:#04x}/{status:#04x}") from error


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
