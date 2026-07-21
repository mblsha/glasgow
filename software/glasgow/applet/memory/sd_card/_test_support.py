import asyncio
import logging
import time

from . import SDCardInterface, _BackendSession
from ._backend import BackendError, BlockBackend
from ._diagnostics import FPGA_DIAGNOSTICS_LAYOUT
from ._gateware_support import BLOCK_SIZE
from ._protocol import BackendStatus


class ImmediateRegister:
    def __init__(self, value=0):
        self.value = value
        self.writes = []
        self.reads = 0

    async def set(self, value):
        self.value = value
        self.writes.append(value)

    def __await__(self):
        async def get():
            self.reads += 1
            return self.value
        return get().__await__()


class FakePipe:
    def __init__(self, request=b""):
        self.request = bytearray(request)
        self.replies = bytearray()
        self.flushes = 0
        self.resets = 0

    async def recv_until(self, delimiter):
        length = self.request.index(delimiter) + len(delimiter)
        data = self.request[:length]
        del self.request[:length]
        return bytes(data)

    async def send(self, data):
        self.replies.extend(data)

    async def flush(self):
        self.flushes += 1

    async def reset(self):
        self.resets += 1
        self.request.clear()
        self.replies.clear()


class MemoryBackend(BlockBackend):
    def __init__(self, image, *, read_only=False):
        self.image = bytearray(image)
        self._read_only = read_only
        self.flushes = 0
        self.closed = False

    @property
    def size(self):
        return len(self.image)

    @property
    def read_only(self):
        return self._read_only

    @property
    def identity(self):
        return 0x1234_5678

    def _check(self, address):
        if address % BLOCK_SIZE or address < 0 or address > self.size - BLOCK_SIZE:
            raise BackendError(BackendStatus.Invalid, "invalid block")

    def read_block(self, address):
        self._check(address)
        return bytes(self.image[address:address + BLOCK_SIZE])

    def write_block(self, address, block):
        self._check(address)
        if self.read_only:
            raise BackendError(BackendStatus.ReadOnly, "read-only backend")
        self.image[address:address + BLOCK_SIZE] = block

    def erase(self, start, end):
        self._check(start)
        self._check(end)
        if start > end:
            raise BackendError(BackendStatus.Invalid, "invalid erase range")
        if self.read_only:
            raise BackendError(BackendStatus.ReadOnly, "read-only backend")
        self.image[start:end + BLOCK_SIZE] = bytes([0xff]) * (end - start + BLOCK_SIZE)

    def flush(self):
        self.flushes += 1

    def close(self):
        self.closed = True


class InlineWorker:
    def __init__(self, backend):
        self.backend = backend
        self.service_count = 0
        self.max_service_latency = 0.0

    async def _call(self, operation, *args):
        started = time.monotonic()
        try:
            return operation(*args)
        finally:
            self.service_count += 1
            self.max_service_latency = max(
                self.max_service_latency, time.monotonic() - started)

    async def read_block(self, address):
        return await self._call(self.backend.read_block, address)

    async def write_block(self, address, block):
        return await self._call(self.backend.write_block, address, block)

    async def erase(self, start, end):
        return await self._call(self.backend.erase, start, end)

    async def flush(self):
        return await self._call(self.backend.flush)

    async def close(self):
        return await self._call(self.backend.close)


def packed_diagnostics(**values):
    packed = 0
    for name in FPGA_DIAGNOSTICS_LAYOUT.members:
        field = FPGA_DIAGNOSTICS_LAYOUT[name]
        packed |= values.get(name, 0) << field.offset
    return packed


class TestSDCardInterface(SDCardInterface):
    """Fully initialized host-interface fixture without constructing Glasgow gateware."""

    __test__ = False

    def __init__(self, backend=None, request=b"", *, generation=0x4321,
            sync_writes=True, worker_factory=InlineWorker):
        self._logger = logging.getLogger(__name__)
        self._level = logging.DEBUG
        self._pipe = FakePipe(request)
        self._read_pipe = FakePipe()
        self._enabled = ImmediateRegister(1 if backend is not None else 0)
        self._generation_reg = ImmediateRegister(generation)
        self._read_only_reg = ImmediateRegister(0)
        self._prime_first_read_reg = ImmediateRegister(0)
        self._image_size = ImmediateRegister(0)
        self._cid = ImmediateRegister(0)
        self._csd = ImmediateRegister(0)
        self._scr = ImmediateRegister(0)
        self._diagnostics_snapshot = ImmediateRegister(0)
        self._session = None if backend is None else _BackendSession(
            backend, worker_factory(backend), sync_writes)
        self._worker_factory = worker_factory
        self._lifecycle_lock = asyncio.Lock()
        self._operation_lock = asyncio.Lock()
        self._generation = generation
        self._active = backend is not None
        self._flush_count = 0
        self._host_backend_errors = 0
