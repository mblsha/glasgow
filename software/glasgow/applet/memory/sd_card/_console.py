import asyncio
from contextlib import suppress
import os
import termios
import tty


__all__ = ["SerialTransport", "TelnetTransport"]


class TelnetTransport:
    """Minimal asyncio Telnet transport with explicit option negotiation."""

    IAC  = 0xff
    DONT = 0xfe
    DO   = 0xfd
    WONT = 0xfc
    WILL = 0xfb
    SB   = 0xfa
    SE   = 0xf0

    BINARY = 0
    ECHO = 1
    SUPPRESS_GO_AHEAD = 3

    def __init__(self, reader, writer):
        self._reader = reader
        self._writer = writer
        self._state = "Data"
        self._command = None

    @classmethod
    async def connect(cls, host, port=23):
        reader, writer = await asyncio.open_connection(host, port)
        return cls(reader, writer)

    async def _negotiate(self, command, option):
        if command == self.WILL:
            response = self.DO if option in (
                self.BINARY, self.ECHO, self.SUPPRESS_GO_AHEAD) else self.DONT
        elif command == self.DO:
            response = self.WILL if option in (
                self.BINARY, self.SUPPRESS_GO_AHEAD) else self.WONT
        elif command == self.WONT:
            response = self.DONT
        else:
            response = self.WONT
        self._writer.write(bytes((self.IAC, response, option)))
        await self._writer.drain()

    async def read_some(self):
        while True:
            raw = await self._reader.read(4096)
            if not raw:
                raise EOFError("telnet disconnected")
            cooked = bytearray()
            for octet in raw:
                if self._state == "Data":
                    if octet == self.IAC:
                        self._state = "IAC"
                    else:
                        cooked.append(octet)
                elif self._state == "IAC":
                    if octet == self.IAC:
                        cooked.append(octet)
                        self._state = "Data"
                    elif octet in (self.WILL, self.WONT, self.DO, self.DONT):
                        self._command = octet
                        self._state = "Negotiate"
                    elif octet == self.SB:
                        self._state = "Subnegotiation"
                    else:
                        self._state = "Data"
                elif self._state == "Negotiate":
                    await self._negotiate(self._command, octet)
                    self._state = "Data"
                elif self._state == "Subnegotiation":
                    if octet == self.IAC:
                        self._state = "Subnegotiation-IAC"
                elif self._state == "Subnegotiation-IAC":
                    if octet == self.SE:
                        self._state = "Data"
                    elif octet != self.IAC:
                        self._state = "Subnegotiation"
            if cooked:
                return bytes(cooked)

    async def send(self, data):
        # Escape IAC bytes embedded in application data.
        self._writer.write(bytes(data).replace(b"\xff", b"\xff\xff"))
        await self._writer.drain()

    async def close(self):
        self._writer.close()
        with suppress(ConnectionError):
            await self._writer.wait_closed()


class SerialTransport:
    """POSIX serial-console transport with the same interface as :class:`TelnetTransport`."""

    def __init__(self, fd, original_attributes):
        self._fd = fd
        self._original_attributes = original_attributes

    @classmethod
    async def connect(cls, path, baud=115200):
        try:
            speed = getattr(termios, f"B{baud}")
        except AttributeError as error:
            raise ValueError(f"unsupported serial baud rate {baud}") from error
        fd = os.open(os.fspath(path), os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            original_attributes = termios.tcgetattr(fd)
            tty.setraw(fd)
            attributes = termios.tcgetattr(fd)
            attributes[4] = speed
            attributes[5] = speed
            termios.tcsetattr(fd, termios.TCSANOW, attributes)
        except BaseException:
            os.close(fd)
            raise
        return cls(fd, original_attributes)

    async def read_some(self):
        loop = asyncio.get_running_loop()
        while True:
            ready = loop.create_future()

            def readable():
                if not ready.done():
                    ready.set_result(None)

            loop.add_reader(self._fd, readable)
            try:
                await ready
                try:
                    data = os.read(self._fd, 4096)
                except BlockingIOError:
                    continue
                if not data:
                    raise EOFError("serial console disconnected")
                return data
            finally:
                loop.remove_reader(self._fd)

    async def send(self, data):
        loop = asyncio.get_running_loop()
        data = memoryview(data)
        while data:
            try:
                length = os.write(self._fd, data)
            except BlockingIOError:
                ready = loop.create_future()

                def writable():
                    if not ready.done():
                        ready.set_result(None)

                loop.add_writer(self._fd, writable)
                try:
                    await ready
                finally:
                    loop.remove_writer(self._fd)
            else:
                data = data[length:]

    async def close(self):
        if self._fd is None:
            return
        with suppress(termios.error):
            termios.tcsetattr(self._fd, termios.TCSANOW, self._original_attributes)
        os.close(self._fd)
        self._fd = None
