import asyncio
import os
import unittest

from ._console import SerialTransport, TelnetTransport
from .e2e_zaurus import E2EError, ZaurusMountState, ZaurusSDController


class ZaurusSDControllerTestCase(unittest.TestCase):
    def test_context_restores_exact_state_and_is_idempotent(self):
        class FakeShell:
            def __init__(self):
                self.commands = []

            async def run(self, command, *, check=True, timeout=None):
                self.commands.append((command, check, timeout))
                if "__GLASGOW_SD_" in command:
                    return 0, (
                        "login noise\n"
                        "__GLASGOW_SD_STATE__\t1\t1\t/dev/mmcda1\tvfat\trw,sync\n")
                return 0, ""

        async def scenario():
            shell = FakeShell()
            controller = ZaurusSDController(
                shell, "/usr/mnt.rom/card", settle_delay=0)
            state = await controller.capture_state()
            self.assertTrue(state.module_loaded)
            self.assertTrue(state.daemon_running)
            self.assertEqual(state.mount, ZaurusMountState(
                "/dev/mmcda1", "vfat", "rw,sync"))

            shell.commands.clear()
            async with controller:
                prepare_commands = len(shell.commands)
                await controller.prepare()
                self.assertEqual(len(shell.commands), prepare_commands)
            self.assertTrue(any("killall sdmgr" in command
                for command, _, _ in shell.commands))
            self.assertTrue(any("rmmod sharp_mmcsd_m" in command
                for command, _, _ in shell.commands))
            self.assertTrue(any("insmod" in command
                for command, _, _ in shell.commands))
            restore_commands = len(shell.commands)
            await controller.restore()
            self.assertEqual(len(shell.commands), restore_commands)
            self.assertTrue(any("/sbin/sdmgr" in command
                for command, _, _ in shell.commands[prepare_commands:]))
            self.assertTrue(any("mount -t vfat -o rw,sync /dev/mmcda1" in command
                for command, _, _ in shell.commands[prepare_commands:]))
            self.assertTrue(all(timeout == 30 for _, _, timeout in shell.commands))

        asyncio.run(scenario())
    def test_unmounted_inactive_state_is_preserved(self):
        class FakeShell:
            def __init__(self):
                self.commands = []

            async def run(self, command, *, check=True, timeout=None):
                self.commands.append(command)
                if "__GLASGOW_SD_" in command:
                    return 0, "__GLASGOW_SD_STATE__\t0\t0\t\n"
                return 0, ""

        async def scenario():
            shell = FakeShell()
            controller = ZaurusSDController(shell, "/usr/mnt.rom/card", settle_delay=0)
            await controller.prepare()
            restore_start = len(shell.commands)
            await controller.restore()
            restored = shell.commands[restore_start:]
            self.assertTrue(any("rmmod sharp_mmcsd_m" in command for command in restored))
            self.assertFalse(any("/sbin/sdmgr;" in command for command in restored))
            self.assertFalse(any("mount -t" in command for command in restored))

        asyncio.run(scenario())

    def test_restore_verifies_remote_state(self):
        class FakeShell:
            def __init__(self):
                self.captures = 0

            async def run(self, command, *, check=True, timeout=None):
                if "__GLASGOW_SD_" in command:
                    self.captures += 1
                    if self.captures == 1:
                        return 0, "__GLASGOW_SD_STATE__\t1\t0\t\n"
                    return 0, "__GLASGOW_SD_STATE__\t0\t0\t\n"
                return 0, ""

        async def scenario():
            controller = ZaurusSDController(
                FakeShell(), "/usr/mnt.rom/card", settle_delay=0)
            await controller.prepare()
            with self.assertRaisesRegex(E2EError, "did not converge"):
                await controller.restore()
            self.assertTrue(controller._restore_needed)

        asyncio.run(scenario())

    def test_restore_retries_transient_state_mismatch(self):
        class FakeShell:
            def __init__(self):
                self.captures = 0

            async def run(self, command, *, check=True, timeout=None):
                if "__GLASGOW_SD_" in command:
                    self.captures += 1
                    if self.captures == 2:
                        return 0, "__GLASGOW_SD_STATE__\t0\t0\t\n"
                    return 0, "__GLASGOW_SD_STATE__\t1\t0\t\n"
                return 0, ""

        async def scenario():
            shell = FakeShell()
            controller = ZaurusSDController(
                shell, "/usr/mnt.rom/card", settle_delay=0)
            await controller.prepare()
            await controller.restore()
            self.assertEqual(shell.captures, 3)
            self.assertFalse(controller._restore_needed)

        asyncio.run(scenario())

    def test_restore_retries_transient_command_failure(self):
        class FakeShell:
            def __init__(self):
                self.fail_next = False

            async def run(self, command, *, check=True, timeout=None):
                if "__GLASGOW_SD_" in command:
                    return 0, "__GLASGOW_SD_STATE__\t1\t0\t\n"
                if self.fail_next:
                    self.fail_next = False
                    raise E2EError("temporary console failure")
                return 0, ""

        async def scenario():
            shell = FakeShell()
            controller = ZaurusSDController(
                shell, "/usr/mnt.rom/card", settle_delay=0)
            await controller.prepare()
            shell.fail_next = True
            await controller.restore()
            self.assertFalse(controller._restore_needed)

        asyncio.run(scenario())

    def test_context_preserves_primary_and_restore_failures(self):
        async def scenario():
            controller = ZaurusSDController(None, "/usr/mnt.rom/card", settle_delay=0)
            primary = ValueError("test failed")
            restore = E2EError("restore failed")

            async def fail_restore():
                raise restore

            controller.restore = fail_restore
            with self.assertRaises(BaseExceptionGroup) as context:
                await controller.__aexit__(ValueError, primary, primary.__traceback__)
            self.assertEqual(context.exception.exceptions, (primary, restore))

        asyncio.run(scenario())


class ConsoleTransportTestCase(unittest.TestCase):
    def test_telnet_username_negotiation(self):
        async def scenario():
            received = asyncio.get_running_loop().create_future()

            async def handler(reader, writer):
                writer.write(bytes((
                    TelnetTransport.IAC, TelnetTransport.DO,
                    TelnetTransport.NEW_ENVIRON,
                )))
                await writer.drain()
                negotiation = await reader.readexactly(3)
                writer.write(bytes((
                    TelnetTransport.IAC, TelnetTransport.SB,
                    TelnetTransport.NEW_ENVIRON, TelnetTransport.ENVIRON_SEND,
                    TelnetTransport.IAC, TelnetTransport.SE,
                )) + b"ready")
                await writer.drain()
                environment = await reader.readuntil(bytes((
                    TelnetTransport.IAC, TelnetTransport.SE,
                )))
                received.set_result((negotiation, environment))
                writer.close()
                await writer.wait_closed()

            server = await asyncio.start_server(handler, "127.0.0.1", 0)
            try:
                port = server.sockets[0].getsockname()[1]
                transport = await TelnetTransport.connect(
                    "127.0.0.1", port, username="zaurus")
                self.assertEqual(await transport.read_some(), b"ready")
                negotiation, environment = await received
                self.assertEqual(negotiation, bytes((
                    TelnetTransport.IAC, TelnetTransport.WILL,
                    TelnetTransport.NEW_ENVIRON,
                )))
                self.assertEqual(environment, bytes((
                    TelnetTransport.IAC, TelnetTransport.SB,
                    TelnetTransport.NEW_ENVIRON, TelnetTransport.ENVIRON_IS,
                    TelnetTransport.ENVIRON_VAR,
                )) + b"USER" + bytes((TelnetTransport.ENVIRON_VALUE,)) + \
                    b"zaurus" + bytes((TelnetTransport.IAC, TelnetTransport.SE)))
                await transport.close()
            finally:
                server.close()
                await server.wait_closed()

        asyncio.run(scenario())

    def test_telnet_negotiation_and_iac_escaping(self):
        async def scenario():
            received = asyncio.get_running_loop().create_future()

            async def handler(reader, writer):
                writer.write(bytes((
                    TelnetTransport.IAC, TelnetTransport.WILL, TelnetTransport.ECHO,
                    TelnetTransport.IAC, TelnetTransport.DO,
                    TelnetTransport.SUPPRESS_GO_AHEAD,
                )) + b"hello\xff\xff!")
                await writer.drain()
                negotiation = await reader.readexactly(6)
                application = await reader.readexactly(4)
                received.set_result((negotiation, application))
                writer.close()
                await writer.wait_closed()

            server = await asyncio.start_server(handler, "127.0.0.1", 0)
            try:
                port = server.sockets[0].getsockname()[1]
                transport = await TelnetTransport.connect("127.0.0.1", port)
                self.assertEqual(await transport.read_some(), b"hello\xff!")
                await transport.send(b"A\xffB")
                negotiation, application = await received
                self.assertEqual(negotiation, bytes((
                    TelnetTransport.IAC, TelnetTransport.DO, TelnetTransport.ECHO,
                    TelnetTransport.IAC, TelnetTransport.WILL,
                    TelnetTransport.SUPPRESS_GO_AHEAD,
                )))
                self.assertEqual(application, b"A\xff\xffB")
                await transport.close()
            finally:
                server.close()
                await server.wait_closed()

        asyncio.run(scenario())

    def test_serial_transport_over_pseudoterminal(self):
        async def scenario():
            master_fd, slave_fd = os.openpty()
            try:
                transport = await SerialTransport.connect(os.ttyname(slave_fd), 9600)
                await transport.send(b"host-to-target")
                self.assertEqual(os.read(master_fd, 14), b"host-to-target")
                os.write(master_fd, b"target-to-host")
                self.assertEqual(await transport.read_some(), b"target-to-host")
                await transport.close()
            finally:
                os.close(master_fd)
                os.close(slave_fd)

        asyncio.run(scenario())
