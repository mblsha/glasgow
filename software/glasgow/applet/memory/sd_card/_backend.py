from abc import ABCMeta, abstractmethod
import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import os
import time

from ._protocol import BLOCK_SIZE, BackendStatus


__all__ = ["BlockBackend", "BackendError", "BackendWorker", "FileBlockBackend"]


class BackendError(Exception):
    def __init__(self, status: BackendStatus, message: str):
        self.status = BackendStatus(status)
        super().__init__(message)


class BlockBackend(metaclass=ABCMeta):
    """Random-access storage used by the host side of the SD emulator."""

    @property
    def preferred_read_ahead_blocks(self) -> int:
        """Efficient contiguous read size advertised to :class:`BackendWorker`.

        Generic backends are deliberately conservative. Backends that can fetch a contiguous
        run substantially more efficiently than repeated single-block calls may opt in by
        overriding this property.
        """
        return 1

    @property
    @abstractmethod
    def size(self) -> int:
        pass

    @property
    @abstractmethod
    def read_only(self) -> bool:
        pass

    @property
    @abstractmethod
    def identity(self) -> int:
        pass

    @abstractmethod
    def read_block(self, address: int) -> bytes:
        pass

    def read_blocks(self, address: int, count: int) -> bytes:
        """Read a contiguous block run, with a correct fallback for non-file backends."""
        return b"".join(
            self.read_block(address + index * BLOCK_SIZE) for index in range(count))

    @abstractmethod
    def write_block(self, address: int, block: bytes) -> None:
        pass

    @abstractmethod
    def erase(self, start: int, end: int) -> None:
        pass

    @abstractmethod
    def flush(self) -> None:
        pass

    @abstractmethod
    def close(self) -> None:
        pass


class BackendWorker:
    """Serialize blocking backend calls on one dedicated worker thread.

    Only one operation may be queued in addition to the operation currently executing. This
    keeps a slow disk or network-backed implementation from accumulating unbounded work while
    preserving the strict ordering required for write-and-flush acknowledgements. A bounded
    host-memory read-ahead window avoids an executor handoff for every sequential SD block; any
    modifying operation invalidates that window before it reaches the backend.
    """

    def __init__(self, backend: BlockBackend, *, read_ahead_blocks=None):
        if read_ahead_blocks is None:
            read_ahead_blocks = backend.preferred_read_ahead_blocks
        if read_ahead_blocks < 1:
            raise ValueError("read_ahead_blocks must be positive")
        self.backend = backend
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="glasgow-sd")
        self._slots = asyncio.Semaphore(2)
        self._ordering = asyncio.Lock()
        self._read_ahead_blocks = read_ahead_blocks
        self._read_cache_address = 0
        self._read_cache = b""
        self._closing = False
        self._closed = False
        self._service_count = 0
        self._max_service_latency = 0.0

    @property
    def read_ahead_blocks(self) -> int:
        return self._read_ahead_blocks

    @property
    def service_count(self) -> int:
        return self._service_count

    @property
    def max_service_latency(self) -> float:
        """Longest blocking backend call observed, in seconds."""
        return self._max_service_latency

    def _invalidate_read_cache(self):
        self._read_cache = b""

    async def _run(self, function, *args):
        loop = asyncio.get_running_loop()
        started_at = time.monotonic()
        future = loop.run_in_executor(self._executor, partial(function, *args))
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            # A running thread cannot be cancelled. Keep the ordered operation active until it
            # finishes so a following read cannot overtake a cancelled write or erase.
            await asyncio.shield(future)
            raise
        finally:
            latency = time.monotonic() - started_at
            self._service_count += 1
            self._max_service_latency = max(self._max_service_latency, latency)

    async def _execute(self, function, *args, _allow_closing=False, _invalidate_cache=False):
        if self._closed or (self._closing and not _allow_closing):
            raise RuntimeError("backend worker is closed")
        async with self._slots:
            if self._closed or (self._closing and not _allow_closing):
                raise RuntimeError("backend worker is closed")
            async with self._ordering:
                if self._closed or (self._closing and not _allow_closing):
                    raise RuntimeError("backend worker is closed")
                if _invalidate_cache:
                    self._invalidate_read_cache()
                return await self._run(function, *args)

    async def read_block(self, address: int) -> bytes:
        if self._closed or self._closing:
            raise RuntimeError("backend worker is closed")
        async with self._slots:
            if self._closed or self._closing:
                raise RuntimeError("backend worker is closed")
            async with self._ordering:
                cache_offset = address - self._read_cache_address
                if address % BLOCK_SIZE == 0 and cache_offset >= 0 and \
                        cache_offset + BLOCK_SIZE <= len(self._read_cache):
                    return self._read_cache[cache_offset:cache_offset + BLOCK_SIZE]

                available_blocks = (self.backend.size - address) // BLOCK_SIZE
                count = min(self._read_ahead_blocks, max(1, available_blocks))
                blocks = await self._run(self.backend.read_blocks, address, count)
                expected_length = count * BLOCK_SIZE
                if len(blocks) != expected_length:
                    raise BackendError(
                        BackendStatus.ShortIO,
                        f"short read-ahead at {address:#x}: "
                        f"received {len(blocks)} bytes, expected {expected_length}")
                self._read_cache_address = address
                self._read_cache = blocks
                return blocks[:BLOCK_SIZE]

    async def write_block(self, address: int, block: bytes) -> None:
        await self._execute(
            self.backend.write_block, address, block, _invalidate_cache=True)

    async def erase(self, start: int, end: int) -> None:
        await self._execute(self.backend.erase, start, end, _invalidate_cache=True)

    async def flush(self) -> None:
        await self._execute(self.backend.flush)

    async def close(self) -> None:
        if self._closed:
            return
        self._closing = True
        try:
            await self._execute(self.backend.close, _allow_closing=True)
        finally:
            self._closed = True
            self._executor.shutdown(wait=True, cancel_futures=True)


class FileBlockBackend(BlockBackend):
    def __init__(self, path: str, *, read_only: bool = False):
        self.path = os.fspath(path)
        flags = os.O_RDONLY if read_only else os.O_RDWR
        self._fd = os.open(self.path, flags)
        stat = os.fstat(self._fd)
        self._size = stat.st_size
        self._identity = (stat.st_ino ^ stat.st_size) & 0xffff_ffff
        self._read_only = read_only

    @property
    def size(self):
        return self._size

    @property
    def identity(self):
        return self._identity

    @property
    def read_only(self):
        return self._read_only

    @property
    def preferred_read_ahead_blocks(self):
        # One pread amortizes the worker handoff and retains a bounded 128 KiB host cache.
        return 256

    def _check_block(self, address: int):
        if address % BLOCK_SIZE or address < 0 or address > self.size - BLOCK_SIZE:
            raise BackendError(
                BackendStatus.Invalid, f"block address outside image: {address:#x}")

    def read_block(self, address: int) -> bytes:
        self._check_block(address)
        try:
            block = os.pread(self._fd, BLOCK_SIZE, address)
        except OSError as error:
            raise BackendError(BackendStatus.IOError, str(error)) from error
        if len(block) != BLOCK_SIZE:
            raise BackendError(BackendStatus.ShortIO, f"short image read at {address:#x}")
        return block

    def read_blocks(self, address: int, count: int) -> bytes:
        if count < 1:
            raise BackendError(BackendStatus.Invalid, "block count must be positive")
        self._check_block(address)
        self._check_block(address + (count - 1) * BLOCK_SIZE)
        length = count * BLOCK_SIZE
        try:
            blocks = os.pread(self._fd, length, address)
        except OSError as error:
            raise BackendError(BackendStatus.IOError, str(error)) from error
        if len(blocks) != length:
            raise BackendError(BackendStatus.ShortIO, f"short image read at {address:#x}")
        return blocks

    def write_block(self, address: int, block: bytes) -> None:
        self._check_block(address)
        if self.read_only:
            raise BackendError(BackendStatus.ReadOnly, "image is read-only")
        if len(block) != BLOCK_SIZE:
            raise BackendError(
                BackendStatus.Invalid, f"block must contain exactly {BLOCK_SIZE} bytes")
        try:
            length = os.pwrite(self._fd, block, address)
        except OSError as error:
            raise BackendError(BackendStatus.IOError, str(error)) from error
        if length != BLOCK_SIZE:
            raise BackendError(BackendStatus.ShortIO, f"short image write at {address:#x}")

    def erase(self, start: int, end: int) -> None:
        self._check_block(start)
        self._check_block(end)
        if start > end:
            raise BackendError(
                BackendStatus.Invalid, f"invalid erase range {start:#x}..{end:#x}")
        if self.read_only:
            raise BackendError(BackendStatus.ReadOnly, "image is read-only")
        erased = bytes([0xff]) * BLOCK_SIZE
        for address in range(start, end + 1, BLOCK_SIZE):
            self.write_block(address, erased)

    def flush(self) -> None:
        if self.read_only:
            return
        try:
            os.fsync(self._fd)
        except OSError as error:
            raise BackendError(BackendStatus.IOError, str(error)) from error

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
