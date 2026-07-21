from enum import IntEnum
import struct


__all__ = [
    "BackendStatus", "HostEvent", "HostReply", "RequestHeader", "ReplyHeader",
]


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


class RequestHeader:
    """Header shared by every FPGA-to-host block request."""

    _STRUCT = struct.Struct("<BHH")
    size = _STRUCT.size

    def __init__(self, opcode: HostEvent, generation: int, request_id: int):
        self.opcode = HostEvent(opcode)
        self.generation = generation
        self.request_id = request_id

    def encode(self) -> bytes:
        return self._STRUCT.pack(self.opcode, self.generation, self.request_id)

    @classmethod
    def decode(cls, value: bytes):
        opcode, generation, request_id = cls._STRUCT.unpack(value)
        return cls(HostEvent(opcode), generation, request_id)


class ReplyHeader:
    """Header shared by every host-to-FPGA block reply."""

    _STRUCT = struct.Struct("<BHHB")
    size = _STRUCT.size

    def __init__(self, opcode: HostReply, generation: int, request_id: int,
            status: BackendStatus = BackendStatus.OK):
        self.opcode = HostReply(opcode)
        self.generation = generation
        self.request_id = request_id
        self.status = BackendStatus(status)

    def encode(self) -> bytes:
        return self._STRUCT.pack(
            self.opcode, self.generation, self.request_id, self.status)

    @classmethod
    def decode(cls, value: bytes):
        opcode, generation, request_id, status = cls._STRUCT.unpack(value)
        return cls(HostReply(opcode), generation, request_id, BackendStatus(status))
