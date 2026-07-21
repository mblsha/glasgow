from abc import ABCMeta, abstractmethod
import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import os

from ._protocol import BackendStatus


__all__ = ["BlockBackend", "BackendError", "BackendWorker", "FileBlockBackend"]


BLOCK_SIZE = 512


class BackendError(Exception):
    def __init__(self, status: BackendStatus, message: str):
        self.status = BackendStatus(status)
        super().__init__(message)


class BlockBackend(metaclass=ABCMeta):
    """Random-access storage used by the host side of the SD emulator."""

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
    preserving the strict ordering required for write-and-flush acknowledgements.
    """

    def __init__(self, backend: BlockBackend):
        self.backend = backend
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="glasgow-sd")
        self._slots = asyncio.Semaphore(2)
        self._closing = False
        self._closed = False

    async def _execute(self, function, *args, _allow_closing=False):
        if self._closed or (self._closing and not _allow_closing):
            raise RuntimeError("backend worker is closed")
        async with self._slots:
            if self._closed or (self._closing and not _allow_closing):
                raise RuntimeError("backend worker is closed")
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                # Amaranth's coroutine simulator implements async/await without an asyncio event
                # loop. Keep its deterministic test backend inline; real applet execution always
                # has a running loop and therefore uses the dedicated worker.
                return function(*args)
            future = loop.run_in_executor(self._executor, partial(function, *args))
            try:
                return await asyncio.shield(future)
            except asyncio.CancelledError:
                # A running thread cannot be cancelled. Keep the bounded slot occupied until it
                # finishes so repeated task cancellation cannot grow the executor's work queue.
                await asyncio.shield(future)
                raise

    async def read_block(self, address: int) -> bytes:
        return await self._execute(self.backend.read_block, address)

    async def write_block(self, address: int, block: bytes) -> None:
        await self._execute(self.backend.write_block, address, block)

    async def erase(self, start: int, end: int) -> None:
        await self._execute(self.backend.erase, start, end)

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
