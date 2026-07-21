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
    NEW_ENVIRON = 39

    ENVIRON_IS = 0
    ENVIRON_SEND = 1
    ENVIRON_VAR = 0
    ENVIRON_VALUE = 1
    ENVIRON_ESC = 2
    ENVIRON_USERVAR = 3

    def __init__(self, reader, writer, *, username=None):
        self._reader = reader
        self._writer = writer
        self._username = username
        self._state = "Data"
        self._command = None
        self._subnegotiation_option = None
        self._subnegotiation = bytearray()

    @classmethod
    async def connect(cls, host, port=23, *, username=None):
        reader, writer = await asyncio.open_connection(host, port)
        return cls(reader, writer, username=username)

    async def _write_protocol(self, data):
        self._writer.write(bytes(data))
        await self._writer.drain()

    async def _negotiate(self, command, option):
        if command == self.WILL:
            response = self.DO if option in (
                self.BINARY, self.ECHO, self.SUPPRESS_GO_AHEAD) else self.DONT
        elif command == self.DO:
            supported = option in (self.BINARY, self.SUPPRESS_GO_AHEAD) or \
                option == self.NEW_ENVIRON and self._username is not None
            response = self.WILL if supported else self.WONT
        elif command == self.WONT:
            response = self.DONT
        else:
            response = self.WONT
        await self._write_protocol((self.IAC, response, option))

    @classmethod
    def _escape_environ(cls, value):
        encoded = bytearray()
        for octet in value.encode():
            if octet in (cls.ENVIRON_VAR, cls.ENVIRON_VALUE,
                    cls.ENVIRON_ESC, cls.ENVIRON_USERVAR):
                encoded.append(cls.ENVIRON_ESC)
            encoded.append(octet)
        return bytes(encoded)

    async def _handle_subnegotiation(self):
        if self._subnegotiation_option != self.NEW_ENVIRON or self._username is None:
            return
        if not self._subnegotiation or self._subnegotiation[0] != self.ENVIRON_SEND:
            return
        payload = bytes((
            self.IAC, self.SB, self.NEW_ENVIRON, self.ENVIRON_IS, self.ENVIRON_VAR,
        )) + self._escape_environ("USER") + bytes((self.ENVIRON_VALUE,)) + \
            self._escape_environ(self._username) + bytes((self.IAC, self.SE))
        await self._write_protocol(payload)

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
                        self._state = "Subnegotiation-Option"
                    else:
                        self._state = "Data"
                elif self._state == "Negotiate":
                    await self._negotiate(self._command, octet)
                    self._state = "Data"
                elif self._state == "Subnegotiation-Option":
                    self._subnegotiation_option = octet
                    self._subnegotiation.clear()
                    self._state = "Subnegotiation"
                elif self._state == "Subnegotiation":
                    if octet == self.IAC:
                        self._state = "Subnegotiation-IAC"
                    else:
                        self._subnegotiation.append(octet)
                elif self._state == "Subnegotiation-IAC":
                    if octet == self.SE:
                        await self._handle_subnegotiation()
                        self._state = "Data"
                    elif octet == self.IAC:
                        self._subnegotiation.append(octet)
                        self._state = "Subnegotiation"
                    else:
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
