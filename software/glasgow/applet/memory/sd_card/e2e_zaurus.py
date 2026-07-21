"""End-to-end validation of ``memory-sd-card`` with a Zaurus SL-C860.

This is deliberately an opt-in hardware harness rather than a unit test. It starts Glasgow,
controls the Zaurus over Telnet or a serial console, reloads the Linux 2.4 SD driver, compares raw
sectors, mounts FAT, and checks a write all the way back in the host image.
"""

import argparse
import asyncio
from contextlib import suppress
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import secrets
import shlex
import struct
import sys
import tempfile
import time

from . import DEFAULT_TRANSFER_SPEED, parse_transfer_speed
from ._fat16 import FAT16Error, FAT16Image, create_fat16_image
from ._console import SerialTransport, TelnetTransport


MODULE = "/lib/modules.rom/2.4.18-rmk7-pxa3-embedix/kernel/drivers/block/sharp_mmcsd_m.o"


class E2EError(Exception):
    pass


@dataclass(frozen=True)
class ZaurusMountState:
    source: str
    filesystem: str
    options: str


@dataclass(frozen=True)
class ZaurusSDState:
    module_loaded: bool
    daemon_running: bool
    mount: ZaurusMountState | None


class ConsoleShell:
    def __init__(self, transport, *, timeout):
        self.transport = transport
        self.timeout = timeout

    @classmethod
    async def connect(cls, host, port=23, *, username="zaurus", timeout=15):
        transport = await TelnetTransport.connect(host, port, username=username)
        shell = cls(transport, timeout=timeout)
        try:
            await shell._establish()
        except BaseException:
            await shell.close()
            raise
        return shell

    @classmethod
    async def connect_serial(cls, path, baud=115200, *, timeout=15):
        transport = await SerialTransport.connect(path, baud)
        shell = cls(transport, timeout=timeout)
        try:
            await shell._establish()
        except BaseException:
            await shell.close()
            raise
        return shell

    async def _establish(self):
        # The Cacko console does not consistently display a prompt. Probe command execution with
        # a marker that does not appear contiguously in the echoed command line.
        await self.send("bash --noprofile --norc")
        token = secrets.token_hex(8)
        marker = f"__GLASGOW_E2E_READY_{token}__"
        await self.send(f"printf '%s%s\\n' '__GLASGOW_E2E_READY_' '{token}__'")
        await self._read_until(marker.encode())

    async def _read_some(self):
        try:
            return await self.transport.read_some()
        except (EOFError, OSError) as error:
            raise E2EError(str(error)) from error

    async def _read_until(self, marker: bytes, *, timeout=None):
        output = bytearray()

        async def receive():
            while marker not in output:
                output.extend(await self._read_some())
            return bytes(output)

        try:
            return await asyncio.wait_for(receive(), timeout or self.timeout)
        except asyncio.TimeoutError as error:
            raise E2EError(
                f"timed out waiting for {marker!r}: {output.decode(errors='replace')}") from error

    async def run(self, command, *, check=True, timeout=None):
        token = secrets.token_hex(8)
        end_marker = f"__GLASGOW_E2E_END_{token}__"
        wrapped = (
            f"echo __GLASGOW_E2E_BEGIN_{token}__; {command}; "
            f"status=$?; echo __GLASGOW_E2E_STATUS_{token}=$status; "
            f"echo {end_marker}")
        await self.send(wrapped)
        status_marker = f"__GLASGOW_E2E_STATUS_{token}="
        output = bytearray()

        async def receive_result():
            pattern = re.compile(re.escape(status_marker) + r"([0-9]+)")
            while True:
                output.extend(await self._read_some())
                decoded = output.decode(errors="replace")
                if (match := pattern.search(decoded)) and \
                        end_marker in decoded[match.end():]:
                    return int(match.group(1)), decoded

        try:
            status, output_text = await asyncio.wait_for(
                receive_result(), timeout or self.timeout)
        except asyncio.TimeoutError as error:
            raise E2EError(
                "timed out waiting for Zaurus command:\n" + output.decode(errors="replace")) \
                from error
        if check and status != 0:
            raise E2EError(f"Zaurus command failed with status {status}:\n{output_text}")
        return status, output_text

    async def send(self, command):
        await self.transport.send(command.encode() + b"\n")

    async def close(self):
        await self.transport.close()


async def connect_after_boot(host, port, *, username, timeout,
        serial_device=None, serial_baud=115200):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            if serial_device is not None:
                return await ConsoleShell.connect_serial(
                    serial_device, serial_baud, timeout=10)
            return await ConsoleShell.connect(host, port, username=username, timeout=15)
        except (OSError, asyncio.TimeoutError, E2EError) as error:
            last_error = error
            await asyncio.sleep(5)
    raise E2EError(
        f"Zaurus did not provide a usable console shell within {timeout:.0f} seconds; "
        f"close any other console session and retry: {last_error}")


def root(command):
    return f"su -c {shlex.quote(command)}"


class ZaurusSDController:
    """Temporarily take over Sharp SD services and restore their exact prior state."""

    def __init__(self, shell, mount_point, *, module=MODULE, settle_delay=2):
        self.shell = shell
        self.mount_point = mount_point
        self.module = module
        self.settle_delay = settle_delay
        self._prepared = False
        self._restore_needed = False
        self._initial_state = None

    async def _run_root(self, command, *, check=True):
        return await self.shell.run(root(command), check=check, timeout=30)

    async def capture_state(self):
        marker = "__GLASGOW_SD_STATE__"
        mount_point = shlex.quote(self.mount_point)
        command = (
            "module=0; grep -q '^sharp_mmcsd_m ' /proc/modules && module=1; "
            "daemon=0; ps | grep '[s]dmgr' >/dev/null 2>&1 && daemon=1; "
            f"mount_line=$(awk -v point={mount_point} "
            "'$2 == point { print $1 \"\\t\" $3 \"\\t\" $4; exit }' /proc/mounts); "
            "printf '%s%s\\t%s\\t%s\\t%s\\n' "
            "'__GLASGOW_SD_' 'STATE__' \"$module\" \"$daemon\" \"$mount_line\"")
        _, output = await self._run_root(command)
        for line in output.splitlines():
            if not line.startswith(marker + "\t"):
                continue
            fields = line.split("\t")
            if len(fields) not in (4, 6) or fields[1] not in ("0", "1") or \
                    fields[2] not in ("0", "1"):
                break
            mount = None
            if len(fields) == 6 and fields[3]:
                mount = ZaurusMountState(
                    source=fields[3], filesystem=fields[4], options=fields[5])
            return ZaurusSDState(
                module_loaded=fields[1] == "1",
                daemon_running=fields[2] == "1",
                mount=mount)
        raise E2EError(f"could not parse Zaurus SD lifecycle state:\n{output}")

    async def unmount(self):
        mount_pattern = shlex.quote(f" {self.mount_point} ")
        mount_point = shlex.quote(self.mount_point)
        await self._run_root(
            f"if grep -F {mount_pattern} /proc/mounts >/dev/null 2>&1; "
            f"then umount {mount_point}; fi")

    async def stop_daemon(self):
        # killall is required because an interrupted run can leave a daemon not named by the
        # current PID file. Keeping it here avoids scattering a destructive process-wide action
        # through setup and cleanup strings.
        await self._run_root("/usr/bin/killall sdmgr >/dev/null 2>&1 || true")
        await self._run_root("rm -f /var/run/sdmgr.pid")

    async def unload_driver(self):
        await self._run_root(
            "if grep -q '^sharp_mmcsd_m ' /proc/modules; "
            "then /sbin/rmmod sharp_mmcsd_m; fi")

    async def load_driver(self):
        await self._run_root(
            "if ! grep -q '^sharp_mmcsd_m ' /proc/modules; "
            f"then /sbin/insmod {shlex.quote(self.module)}; fi")

    async def start_daemon(self):
        await self._run_root(
            "if ! ps | grep '[s]dmgr' >/dev/null 2>&1; then "
            "rm -f /var/run/sdmgr.pid; /sbin/sdmgr; fi")

    async def restore_mount(self, mount):
        if mount is None:
            return
        mount_pattern = shlex.quote(f" {self.mount_point} ")
        await self._run_root(
            f"mkdir -p {shlex.quote(self.mount_point)}; "
            f"if ! grep -F {mount_pattern} /proc/mounts >/dev/null 2>&1; then "
            f"mount -t {shlex.quote(mount.filesystem)} -o {shlex.quote(mount.options)} "
            f"{shlex.quote(mount.source)} {shlex.quote(self.mount_point)}; fi")

    async def prepare(self):
        if self._prepared:
            return
        self._initial_state = await self.capture_state()
        self._restore_needed = True
        try:
            await self.unmount()
            await self.stop_daemon()
            if self.settle_delay:
                await asyncio.sleep(self.settle_delay)
            await self.unload_driver()
            if self.settle_delay:
                await asyncio.sleep(self.settle_delay)
            await self.load_driver()
            self._prepared = True
        except BaseException:
            await self.restore()
            raise

    async def restore(self):
        if not self._restore_needed:
            return
        state = self._initial_state
        if state is None:
            raise E2EError("Zaurus SD lifecycle restoration has no captured state")
        await self.unmount()
        await self.stop_daemon()
        if state.module_loaded:
            await self.load_driver()
        else:
            await self.unload_driver()
        if state.daemon_running:
            await self.start_daemon()
        if state.mount is None:
            # sdmgr can mount opportunistically while starting; preserve an originally unmounted
            # slot as well as an originally mounted one.
            await self.unmount()
        else:
            await self.restore_mount(state.mount)
        self._prepared = False
        self._restore_needed = False
        self._initial_state = None

    async def __aenter__(self):
        await self.prepare()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        await self.restore()
        return False


def md5(block):
    return hashlib.md5(block).hexdigest()


def image_sectors(path):
    with open(path, "rb") as image:
        mbr = image.read(512)
        if len(mbr) != 512:
            raise E2EError("image is shorter than one sector")
        partition_lba, = struct.unpack_from("<I", mbr, 446 + 8)
        if partition_lba == 0:
            raise E2EError("first MBR partition has a zero start LBA")
        image.seek(partition_lba * 512)
        bpb = image.read(512)
        if len(bpb) != 512:
            raise E2EError("first partition boot sector is outside the image")
    return partition_lba, mbr, bpb


async def start_glasgow(args):
    command = [args.glasgow]
    if args.verbose_glasgow:
        command.append("-v")
    command += [
        "run", "memory-sd-card", "-V", args.voltage,
        "--clk", args.clk, "--cmd", args.cmd, "--dat", args.dat,
    ]
    if args.volatile_writes:
        command.append("--volatile-writes")
    if args.transfer_speed != DEFAULT_TRANSFER_SPEED:
        command += ["--transfer-speed", str(args.transfer_speed)]
    command.append(os.fspath(args.image))
    process = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)

    output = bytearray()

    async def wait_ready():
        while b"serving " not in output:
            chunk = await process.stdout.read(1024)
            if not chunk:
                raise E2EError(
                    "Glasgow exited before serving the image:\n" +
                    output.decode(errors="replace"))
            output.extend(chunk)

    try:
        await asyncio.wait_for(wait_ready(), args.glasgow_timeout)
    except BaseException:
        if process.returncode is None:
            process.terminate()
            await process.wait()
        raise

    async def log_output():
        while chunk := await process.stdout.read(1024):
            sys.stderr.buffer.write(chunk)
            sys.stderr.buffer.flush()

    log_task = asyncio.create_task(log_output())
    return process, log_task


async def stop_glasgow(process, log_task, glasgow):
    if process is not None and process.returncode is None:
        process.send_signal(2)
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(process.wait(), 10)
    if process is not None and process.returncode is None:
        process.terminate()
        await process.wait()
    if log_task is not None:
        log_task.cancel()
        with suppress(asyncio.CancelledError):
            await log_task
    safe = await asyncio.create_subprocess_exec(
        glasgow, "safe", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    await safe.communicate()


async def verify(args, shell):
    partition_lba, host_mbr, host_bpb = image_sectors(args.image)

    raw_command = (
        "rm -f /tmp/glasgow-mbr /tmp/glasgow-bpb; "
        "dd if=/dev/mmcda of=/tmp/glasgow-mbr bs=512 count=1 && "
        f"dd if=/dev/mmcda of=/tmp/glasgow-bpb bs=512 skip={partition_lba} count=1 && "
        "md5sum /tmp/glasgow-mbr /tmp/glasgow-bpb; result=$?; "
        "test $result -eq 0 || dmesg | tail -40; exit $result")
    status, output = await shell.run(root(raw_command), check=False, timeout=45)
    if status != 0:
        raise E2EError(f"raw Zaurus sector read failed:\n{output}")
    expected_mbr = md5(host_mbr)
    expected_bpb = md5(host_bpb)
    if expected_mbr not in output or expected_bpb not in output:
        raise E2EError(
            "raw Zaurus sectors do not match the host image: "
            f"expected MBR {expected_mbr}, BPB {expected_bpb}\n{output}")

    write_marker = f"zaurus-glasgow-e2e-{secrets.token_hex(8)}"
    mount_command = (
        f"mkdir -p {shlex.quote(args.mount_point)}; "
        f"mount -t vfat /dev/mmcda1 {shlex.quote(args.mount_point)}; "
        f"grep -F {shlex.quote(args.host_marker)} "
        f"{shlex.quote(args.mount_point + '/' + args.host_file)}; "
        f"printf '%s\\n' {shlex.quote(write_marker)} > "
        f"{shlex.quote(args.mount_point + '/' + args.write_file)}; sync; "
        f"grep -F {shlex.quote(write_marker)} "
        f"{shlex.quote(args.mount_point + '/' + args.write_file)}; "
        f"umount {shlex.quote(args.mount_point)}")
    await shell.run(root(mount_command), timeout=60)

    try:
        image = FAT16Image.from_path(args.image)
        written = image.read_file(args.write_file)
    except FAT16Error as error:
        raise E2EError(f"host image failed FAT16 validation after Zaurus write: {error}") \
            from error
    if written != (write_marker + "\n").encode():
        raise E2EError(
            f"Zaurus file contents do not match host image: {written!r}")
    return expected_mbr, expected_bpb, write_marker


async def async_main(args):
    process = log_task = shell = controller = None
    try:
        process, log_task = await start_glasgow(args)
        shell = await connect_after_boot(
            args.host, args.port, username=args.username, timeout=45,
            serial_device=args.serial_device, serial_baud=args.serial_baud)
        if args.reboot:
            await shell.send(root("sync; /sbin/reboot"))
            await shell.close()
            shell = None
            print(f"waiting up to {args.reboot_timeout:.0f} seconds for the Zaurus reboot")
            shell = await connect_after_boot(
                args.host, args.port, username=args.username, timeout=args.reboot_timeout,
                serial_device=args.serial_device, serial_baud=args.serial_baud)

        controller = ZaurusSDController(shell, args.mount_point)
        async with controller:
            mbr, bpb, marker = await verify(args, shell)
        print(f"PASS: MBR {mbr}, BPB {bpb}, write marker {marker}")
    finally:
        if shell is not None:
            if controller is not None:
                with suppress(Exception):
                    await controller.restore()
            await shell.close()
        await stop_glasgow(process, log_task, args.glasgow)


def make_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", nargs="?", type=Path,
        help="prepared FAT16 image containing HOST-FILE (default: generate a deterministic image)")
    parser.add_argument("--host", default="192.168.10.2", help="Zaurus telnet address")
    parser.add_argument("--port", type=int, default=23, help="Zaurus telnet port")
    parser.add_argument("--username", default="zaurus",
        help="Zaurus Telnet username exported with NEW-ENVIRON (default: zaurus)")
    parser.add_argument("--serial-device",
        help="use a directly attached serial console instead of Telnet")
    parser.add_argument("--serial-baud", type=int, default=115200,
        help="serial console baud rate (default: 115200)")
    parser.add_argument("--host-file", default="HOST.TXT",
        help="file already present in the image")
    parser.add_argument("--host-marker", default="glasgow-host-marker",
        help="text expected in HOST-FILE")
    parser.add_argument("--write-file", default="ZAURUS.TXT",
        help="file the Zaurus creates during the test")
    parser.add_argument("--mount-point", default="/usr/mnt.rom/card")
    parser.add_argument("--reboot", action="store_true",
        help="reboot after starting Glasgow to exercise automatic power-cycle recovery")
    parser.add_argument("--reboot-timeout", type=float, default=240,
        help="seconds to wait for the console after reboot (default: 240)")
    parser.add_argument("--glasgow", default="glasgow", help="Glasgow executable")
    parser.add_argument("--glasgow-timeout", type=float, default=90)
    parser.add_argument("--verbose-glasgow", action="store_true",
        help="enable per-block Glasgow debug logging during the hardware test")
    parser.add_argument("--voltage", default="B=SB")
    parser.add_argument("--clk", default="B2")
    parser.add_argument("--cmd", default="B5")
    parser.add_argument("--dat", default="B1,B0,B7,B6")
    parser.add_argument("--volatile-writes", action="store_true")
    parser.add_argument("--transfer-speed", type=parse_transfer_speed,
        default=DEFAULT_TRANSFER_SPEED, metavar="RATE",
        help="make Glasgow advertise a CSD transfer speed such as 25m or 5m (default: 25m)")
    return parser


def main():
    args = make_parser().parse_args()
    if args.reboot_timeout < 120:
        raise SystemExit("--reboot-timeout must be at least 120 seconds for this Zaurus")
    with tempfile.TemporaryDirectory(prefix="glasgow-sd-e2e-") as temporary_directory:
        if args.image is None:
            args.image = Path(temporary_directory) / "zaurus-fat16.img"
            create_fat16_image(args.image, {
                args.host_file: (args.host_marker + "\n").encode(),
            })
            print(f"generated deterministic FAT16 image {args.image}")
        try:
            asyncio.run(async_main(args))
        except (E2EError, FAT16Error, OSError, ValueError, asyncio.TimeoutError) as error:
            raise SystemExit(f"FAIL: {error}") from error


if __name__ == "__main__":
    main()
