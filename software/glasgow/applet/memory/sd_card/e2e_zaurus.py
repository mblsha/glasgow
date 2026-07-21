"""End-to-end validation of ``memory-sd-card`` with a Zaurus SL-C860.

This is deliberately an opt-in hardware harness rather than a unit test. It starts Glasgow,
controls the Zaurus over telnet, reloads the Linux 2.4 SD driver, compares raw sectors, mounts FAT,
and checks a write all the way back in the host image.
"""

import argparse
import asyncio
from contextlib import suppress
import hashlib
import os
from pathlib import Path
import pty
import re
import secrets
import shlex
import struct
import sys
import time


MODULE = "/lib/modules.rom/2.4.18-rmk7-pxa3-embedix/kernel/drivers/block/sharp_mmcsd_m.o"


class E2EError(Exception):
    pass


class TelnetShell:
    def __init__(self, process, master_fd, *, timeout):
        self.process = process
        self.master_fd = master_fd
        self.timeout = timeout

    @classmethod
    async def connect(cls, host, port=23, *, timeout=15):
        master_fd, slave_fd = pty.openpty()
        try:
            process = await asyncio.create_subprocess_exec(
                "telnet", host, str(port),
                stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                start_new_session=True)
        finally:
            os.close(slave_fd)
        shell = cls(process, master_fd, timeout=timeout)
        try:
            await shell._read_until(b"Escape character is")
            await shell.send("bash")
            await shell._read_until(b"$")
        except BaseException:
            await shell.close()
            raise
        return shell

    async def _read_some(self):
        loop = asyncio.get_running_loop()
        ready = loop.create_future()

        def readable():
            if not ready.done():
                ready.set_result(None)

        loop.add_reader(self.master_fd, readable)
        try:
            await ready
            try:
                chunk = os.read(self.master_fd, 4096)
            except OSError as error:
                raise E2EError("telnet disconnected") from error
            if not chunk:
                raise E2EError("telnet disconnected")
            return chunk
        finally:
            loop.remove_reader(self.master_fd)

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
        data = command.encode() + b"\n"
        while data:
            try:
                length = os.write(self.master_fd, data)
            except BlockingIOError:
                await asyncio.sleep(0)
            else:
                data = data[length:]

    async def close(self):
        if self.process.returncode is None:
            with suppress(OSError):
                os.write(self.master_fd, b"\x1dquit\n")
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.process.wait(), 2)
        if self.process.returncode is None:
            self.process.terminate()
            await self.process.wait()
        with suppress(OSError):
            os.close(self.master_fd)


async def connect_after_boot(host, port, *, timeout):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            return await TelnetShell.connect(host, port, timeout=10)
        except (OSError, asyncio.TimeoutError, E2EError) as error:
            last_error = error
            await asyncio.sleep(5)
    raise E2EError(f"Zaurus did not accept telnet within {timeout:.0f} seconds: {last_error}")


def root(command):
    return f"su -c {shlex.quote(command)}"


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
    command = [
        args.glasgow, "-v", "run", "memory-sd-card", "-V", args.voltage,
        "--clk", args.clk, "--cmd", args.cmd, "--dat", args.dat,
    ]
    if args.volatile_writes:
        command.append("--volatile-writes")
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


async def reload_driver(shell):
    command = (
        "SDPID=$(cat /var/run/sdmgr.pid 2>/dev/null); "
        "test -z \"$SDPID\" || kill \"$SDPID\"; "
        "sleep 2; rmmod sharp_mmcsd_m 2>/dev/null || true; sleep 2; "
        f"insmod {shlex.quote(MODULE)}")
    await shell.run(root(command), timeout=30)


async def restore_driver(shell, mount_point):
    command = (
        f"umount {shlex.quote(mount_point)} 2>/dev/null || true; "
        "SDPID=$(cat /var/run/sdmgr.pid 2>/dev/null); "
        "test -z \"$SDPID\" || kill \"$SDPID\" 2>/dev/null || true; "
        "rm -f /var/run/sdmgr.pid; /sbin/sdmgr")
    await shell.run(root(command), check=False, timeout=30)


async def verify(args, shell):
    partition_lba, host_mbr, host_bpb = image_sectors(args.image)
    await reload_driver(shell)

    raw_command = (
        "dd if=/dev/mmcda of=/tmp/glasgow-mbr bs=512 count=1; "
        f"dd if=/dev/mmcda of=/tmp/glasgow-bpb bs=512 skip={partition_lba} count=1; "
        "md5sum /tmp/glasgow-mbr /tmp/glasgow-bpb")
    _, output = await shell.run(root(raw_command), timeout=45)
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

    image = Path(args.image).read_bytes()
    if write_marker.encode() not in image:
        raise E2EError("Zaurus write marker was not found in the host image after unmount")
    return expected_mbr, expected_bpb, write_marker


async def async_main(args):
    process = log_task = shell = None
    driver_restored = False
    try:
        process, log_task = await start_glasgow(args)
        shell = await connect_after_boot(args.host, args.port, timeout=30)
        if args.reboot:
            await shell.send(root("sync; /sbin/reboot"))
            await shell.close()
            shell = None
            print(f"waiting up to {args.reboot_timeout:.0f} seconds for the Zaurus reboot")
            shell = await connect_after_boot(
                args.host, args.port, timeout=args.reboot_timeout)

        mbr, bpb, marker = await verify(args, shell)
        await restore_driver(shell, args.mount_point)
        driver_restored = True
        print(f"PASS: MBR {mbr}, BPB {bpb}, write marker {marker}")
    finally:
        if shell is not None:
            if not driver_restored:
                with suppress(Exception):
                    await restore_driver(shell, args.mount_point)
            await shell.close()
        await stop_glasgow(process, log_task, args.glasgow)


def make_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path, help="prepared FAT16 image containing HOST-FILE")
    parser.add_argument("--host", default="192.168.10.2", help="Zaurus telnet address")
    parser.add_argument("--port", type=int, default=23, help="Zaurus telnet port")
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
        help="seconds to wait for telnet after reboot (default: 240)")
    parser.add_argument("--glasgow", default="glasgow", help="Glasgow executable")
    parser.add_argument("--glasgow-timeout", type=float, default=90)
    parser.add_argument("--voltage", default="B=SB")
    parser.add_argument("--clk", default="B2")
    parser.add_argument("--cmd", default="B5")
    parser.add_argument("--dat", default="B1,B0,B7,B6")
    parser.add_argument("--volatile-writes", action="store_true")
    return parser


def main():
    args = make_parser().parse_args()
    if args.reboot_timeout < 120:
        raise SystemExit("--reboot-timeout must be at least 120 seconds for this Zaurus")
    try:
        asyncio.run(async_main(args))
    except (E2EError, OSError, asyncio.TimeoutError) as error:
        raise SystemExit(f"FAIL: {error}") from error


if __name__ == "__main__":
    main()
