"""Reserved-LBA console transport for the host-backed SD card applet."""

import asyncio
import binascii
from collections import OrderedDict, deque
from dataclasses import dataclass
from enum import IntEnum, IntFlag
import errno
import os
from pathlib import Path
import pty
import secrets
import struct
import tty

from ._backend import BackendError
from ._protocol import BLOCK_SIZE, BackendStatus


MAGIC = b"GSCON001"
VERSION = 1
PARTITION_TYPE = 0xDA
PARTITION_INDEX = 3
FRAME_HEADER = struct.Struct("<8sHBBQQQHH")
CRC_OFFSET = BLOCK_SIZE - 4
PAYLOAD_SIZE = CRC_OFFSET - FRAME_HEADER.size


class ConsoleFrameKind(IntEnum):
    Control = 1
    TargetOutput = 2
    HostInput = 3
    TargetAck = 4
    Empty = 5


class ConsoleFrameFlag(IntFlag):
    HostReady = 1 << 0
    Overflow = 1 << 1
    SequenceGap = 1 << 2


@dataclass(frozen=True)
class ConsoleFrame:
    kind: ConsoleFrameKind
    session: int
    sequence: int = 0
    acknowledgement: int = 0
    payload: bytes = b""
    flags: int = 0
    status: int = 0

    def encode(self) -> bytes:
        payload = bytes(self.payload)
        if len(payload) > PAYLOAD_SIZE:
            raise ValueError(f"console payload exceeds {PAYLOAD_SIZE} bytes")
        block = bytearray(BLOCK_SIZE)
        FRAME_HEADER.pack_into(
            block, 0, MAGIC, VERSION, int(self.kind), self.flags,
            self.session, self.sequence, self.acknowledgement,
            len(payload), self.status)
        block[FRAME_HEADER.size:FRAME_HEADER.size + len(payload)] = payload
        struct.pack_into("<I", block, CRC_OFFSET,
            binascii.crc32(block[:CRC_OFFSET]) & 0xffff_ffff)
        return bytes(block)

    @classmethod
    def decode(cls, block: bytes) -> "ConsoleFrame":
        if len(block) != BLOCK_SIZE:
            raise ValueError("console frame is not exactly one SD block")
        expected_crc, = struct.unpack_from("<I", block, CRC_OFFSET)
        actual_crc = binascii.crc32(block[:CRC_OFFSET]) & 0xffff_ffff
        if actual_crc != expected_crc:
            raise ValueError("console frame CRC mismatch")
        (magic, version, kind, flags, session, sequence, acknowledgement,
         payload_length, status) = FRAME_HEADER.unpack_from(block)
        if magic != MAGIC:
            raise ValueError("console frame magic mismatch")
        if version != VERSION:
            raise ValueError(f"unsupported console frame version {version}")
        if payload_length > PAYLOAD_SIZE:
            raise ValueError("console frame payload length is invalid")
        try:
            kind = ConsoleFrameKind(kind)
        except ValueError as error:
            raise ValueError("console frame kind is invalid") from error
        payload = block[FRAME_HEADER.size:FRAME_HEADER.size + payload_length]
        return cls(kind, session, sequence, acknowledgement, payload, flags, status)


@dataclass(frozen=True)
class SidebandPartition:
    start_lba: int
    sectors: int

    @property
    def start(self) -> int:
        return self.start_lba * BLOCK_SIZE

    @property
    def size(self) -> int:
        return self.sectors * BLOCK_SIZE

    @classmethod
    def from_image(cls, image_path: str | os.PathLike[str]) -> "SidebandPartition":
        path = Path(image_path)
        with path.open("rb") as image:
            header = image.read(BLOCK_SIZE)
            image.seek(0, os.SEEK_END)
            image_size = image.tell()
        if len(header) != BLOCK_SIZE or header[510:512] != b"\x55\xaa":
            raise ValueError("console image does not contain a valid MBR")
        if image_size % BLOCK_SIZE:
            raise ValueError("console image is not block aligned")

        entries = []
        for index in range(4):
            entry = header[446 + index * 16:446 + (index + 1) * 16]
            entries.append((entry[0], entry[4],
                int.from_bytes(entry[8:12], "little"),
                int.from_bytes(entry[12:16], "little")))
        bootable, kind, start_lba, sectors = entries[PARTITION_INDEX]
        if bootable != 0 or kind != PARTITION_TYPE or start_lba == 0 or sectors < 4:
            raise ValueError(
                "fourth MBR partition must be a non-bootable type-0xDA console partition "
                "of at least four sectors")
        total_sectors = image_size // BLOCK_SIZE
        if start_lba + sectors != total_sectors:
            raise ValueError("console partition must end at the image boundary")
        for index, (_, other_kind, other_start, other_sectors) in enumerate(entries[:3]):
            if other_kind == 0 or other_sectors == 0:
                continue
            if other_start < start_lba + sectors and start_lba < other_start + other_sectors:
                raise ValueError(f"console partition overlaps MBR partition {index + 1}")
        return cls(start_lba, sectors)


class SidebandConsole:
    """In-memory, retry-safe console mailbox backed by a persistent host PTY."""

    CONTROL_SECTOR = 0
    OUTPUT_SECTOR = 1
    INPUT_SECTOR = 2
    ACK_SECTOR = 3
    MAX_PENDING_BYTES = 1 << 20
    REMEMBERED_OUTPUTS = 256

    def __init__(self, logger, partition: SidebandPartition):
        self._logger = logger
        self.partition = partition
        self.session = secrets.randbits(64) or 1
        self._last_output_sequence = 0
        self._output_fingerprints = OrderedDict()
        self._target_output = bytearray()
        self._input_staging = bytearray()
        self._input_frames = deque()
        self._next_input_sequence = 1
        self._last_input_ack = 0
        self._overflow = False
        self._sequence_gap = False
        self._primary_fd = None
        self._secondary_fd = None
        self._pty_name = None
        self._pty_link = None

    @property
    def pty_name(self):
        return self._pty_name

    def handles(self, address: int) -> bool:
        return self.partition.start <= address < self.partition.start + self.partition.size

    def overlaps(self, start: int, end: int) -> bool:
        return start < self.partition.start + self.partition.size and end >= self.partition.start

    def _sector(self, address: int) -> int:
        if address % BLOCK_SIZE or not self.handles(address):
            raise BackendError(BackendStatus.Invalid,
                f"address outside console partition: {address:#x}")
        return (address - self.partition.start) // BLOCK_SIZE

    def _flags(self) -> int:
        flags = ConsoleFrameFlag.HostReady if self._primary_fd is not None else 0
        if self._overflow:
            flags |= ConsoleFrameFlag.Overflow
        if self._sequence_gap:
            flags |= ConsoleFrameFlag.SequenceGap
        return int(flags)

    def _materialize_input(self):
        if not self._input_frames and self._input_staging:
            payload = bytes(self._input_staging[:PAYLOAD_SIZE])
            del self._input_staging[:len(payload)]
            frame = ConsoleFrame(
                ConsoleFrameKind.HostInput, self.session,
                sequence=self._next_input_sequence,
                acknowledgement=self._last_output_sequence,
                payload=payload, flags=self._flags())
            self._next_input_sequence += 1
            self._input_frames.append(frame)

    def read_block(self, address: int) -> bytes:
        sector = self._sector(address)
        if sector == self.CONTROL_SECTOR:
            self._materialize_input()
            input_sequence = self._input_frames[0].sequence if self._input_frames else 0
            return ConsoleFrame(
                ConsoleFrameKind.Control, self.session,
                sequence=input_sequence,
                acknowledgement=self._last_output_sequence,
                flags=self._flags(),
                status=min(len(self._input_staging) + sum(
                    len(frame.payload) for frame in self._input_frames), 0xffff)).encode()
        if sector == self.INPUT_SECTOR:
            self._materialize_input()
            if self._input_frames:
                return self._input_frames[0].encode()
            return ConsoleFrame(
                ConsoleFrameKind.Empty, self.session,
                acknowledgement=self._last_output_sequence,
                flags=self._flags()).encode()
        raise BackendError(BackendStatus.Invalid,
            f"console sector {sector} is not readable")

    def _accept_output(self, frame: ConsoleFrame, block: bytes):
        if frame.session != self.session or frame.sequence == 0:
            raise BackendError(BackendStatus.Invalid, "stale console output session or sequence")
        fingerprint, = struct.unpack_from("<I", block, CRC_OFFSET)
        prior = self._output_fingerprints.get(frame.sequence)
        if prior is not None:
            if prior != fingerprint:
                raise BackendError(BackendStatus.Invalid,
                    "duplicate console output sequence has different contents")
            return
        if frame.sequence <= self._last_output_sequence:
            return
        if frame.sequence != self._last_output_sequence + 1:
            self._sequence_gap = True
            marker = (f"\r\n[glasgow-sd-console: target output sequence gap "
                      f"{self._last_output_sequence + 1}..{frame.sequence - 1}]\r\n").encode()
            self._append_target_output(marker)
        self._append_target_output(frame.payload)
        self._last_output_sequence = frame.sequence
        self._output_fingerprints[frame.sequence] = fingerprint
        self._output_fingerprints.move_to_end(frame.sequence)
        while len(self._output_fingerprints) > self.REMEMBERED_OUTPUTS:
            self._output_fingerprints.popitem(last=False)

    def _accept_ack(self, frame: ConsoleFrame):
        if frame.session != self.session:
            raise BackendError(BackendStatus.Invalid, "stale console acknowledgement session")
        sequence = frame.acknowledgement
        if sequence <= self._last_input_ack:
            return
        self._materialize_input()
        if not self._input_frames or sequence != self._input_frames[0].sequence:
            raise BackendError(BackendStatus.Invalid,
                f"console acknowledgement {sequence} is not the pending input")
        self._input_frames.popleft()
        self._last_input_ack = sequence

    def write_block(self, address: int, block: bytes) -> None:
        sector = self._sector(address)
        try:
            frame = ConsoleFrame.decode(block)
        except ValueError as error:
            raise BackendError(BackendStatus.Invalid, str(error)) from error
        if sector == self.OUTPUT_SECTOR and frame.kind == ConsoleFrameKind.TargetOutput:
            self._accept_output(frame, block)
            return
        if sector == self.ACK_SECTOR and frame.kind == ConsoleFrameKind.TargetAck:
            self._accept_ack(frame)
            return
        raise BackendError(BackendStatus.Invalid,
            f"console frame kind {frame.kind.name} is invalid for sector {sector}")

    def queue_host_input(self, data: bytes):
        data = bytes(data)
        queued = len(self._input_staging) + sum(
            len(frame.payload) for frame in self._input_frames)
        available = self.MAX_PENDING_BYTES - queued
        if len(data) > available:
            self._overflow = True
            data = data[:max(0, available)]
        self._input_staging.extend(data)

    def _append_target_output(self, data: bytes):
        self._target_output.extend(data)
        excess = len(self._target_output) - self.MAX_PENDING_BYTES
        if excess > 0:
            del self._target_output[:excess]
            self._overflow = True

    def open_pty(self, link_path: str | os.PathLike[str] | None = None):
        if self._primary_fd is not None:
            raise RuntimeError("console PTY is already open")
        primary, secondary = pty.openpty()
        os.set_blocking(primary, False)
        tty.setraw(secondary)
        self._primary_fd = primary
        self._secondary_fd = secondary
        self._pty_name = os.ttyname(secondary)
        if link_path is not None:
            link = Path(link_path)
            if os.path.lexists(link):
                if not link.is_symlink():
                    self.close()
                    raise FileExistsError(errno.EEXIST,
                        "refusing to replace non-symlink console path", str(link))
                link.unlink()
            link.symlink_to(self._pty_name)
            self._pty_link = link
        self._logger.info("SD sideband console PTY: %s%s", self._pty_name,
            f" (linked at {self._pty_link})" if self._pty_link is not None else "")
        return self._pty_name

    async def serve_pty(self):
        if self._primary_fd is None:
            raise RuntimeError("console PTY is not open")
        while True:
            progressed = False
            try:
                data = os.read(self._primary_fd, 4096)
            except BlockingIOError:
                pass
            else:
                if data:
                    self.queue_host_input(data)
                    progressed = True
            if self._target_output:
                try:
                    count = os.write(self._primary_fd, self._target_output[:4096])
                except BlockingIOError:
                    pass
                else:
                    del self._target_output[:count]
                    progressed = True
            await asyncio.sleep(0 if progressed else 0.01)

    def close(self):
        if self._pty_link is not None:
            try:
                if self._pty_link.is_symlink() and os.readlink(self._pty_link) == self._pty_name:
                    self._pty_link.unlink()
            except FileNotFoundError:
                pass
            self._pty_link = None
        for name in ("_primary_fd", "_secondary_fd"):
            fd = getattr(self, name)
            if fd is not None:
                os.close(fd)
                setattr(self, name, None)
        self._pty_name = None
