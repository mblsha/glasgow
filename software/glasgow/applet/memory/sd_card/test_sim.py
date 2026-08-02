from collections import deque
import os
import random
import tempfile
import unittest

from glasgow.simulation.assembly import SimulationAssembly

from . import BLOCK_SIZE, build_cid, build_csd, build_scr, crc16
from .bfm import SDHostBFM
from ._backend import BackendError
from ._data import READ_NAC_CLOCKS
from ._protocol import BackendStatus
from ._response import RESPONSE_GUARD_CLOCKS
from ._test_support import InlineWorker
from ._diagnostics import decode_fpga_diagnostics


R1_PERSISTENT_ERROR_MASK = sum(1 << bit for bit in range(19, 32))


class SimHostServiceAgent:
    """Serve framed backend requests independently of SD host bus activity."""

    def __init__(self, interface, sd_clock, *, stalls=()):
        self.interface = interface
        self.sd_clock = sd_clock
        self.stalls = deque(stalls)
        self._stall_remaining = None
        self.requests_started = 0
        self.requests_completed = 0

    async def run(self, ctx, *, stopped):
        while not stopped():
            await ctx.posedge(self.sd_clock.i)
            if stopped():
                break
            if not self.interface._pipe.flush_readable:
                continue
            if self._stall_remaining is None:
                self._stall_remaining = self.stalls.popleft() if self.stalls else 0
            if self._stall_remaining:
                self._stall_remaining -= 1
                continue
            self.requests_started += 1
            await self.interface.serve_once()
            self.requests_completed += 1
            self._stall_remaining = None


class SDCardSimulationTestMixin:
    async def _initialize_sim_card(self, applet, bfm, image, payload, *, transfer_speed):
        # Let the enable synchronizer cross into the externally clocked SD domain.
        await bfm.idle(4)

        # Configuration is snapshotted atomically before command sampling. Mutating the source
        # register while enabled must not tear or alter the active 128-bit CID.
        await applet.sd_card_iface._cid.set(0)
        await bfm.send_command(0)
        for _ in range(4):
            command_oe, _, _ = await bfm.cycle()
            self.assertEqual(command_oe, 0)

        await bfm.send_command(1, 0x00ff_c000)
        for _ in range(64):
            command_oe, _, _ = await bfm.cycle()
            self.assertEqual(command_oe, 0)

        await bfm.send_command(55)
        self.assertEqual(await applet.sd_card_iface.last_command, 55)
        bfm.assert_r1(await bfm.receive_response(48), 55)

        await bfm.send_command(41, 0x00ff_8000)
        r3 = await bfm.receive_response(48)
        self.assertEqual(r3 >> 46, 0)
        self.assertEqual((r3 >> 40) & 0x3f, 0x3f)
        self.assertEqual((r3 >> 8) & 0xffff_ffff, 0x80ff_8000)
        self.assertEqual(r3 & 0xff, 0xff)

        stat = os.fstat(image.fileno())
        cid = build_cid(serial=(stat.st_ino ^ stat.st_size) & 0xffff_ffff)
        csd = build_csd(len(payload), transfer_speed=transfer_speed)

        await bfm.send_command(2)
        r2 = await bfm.receive_response(136)
        self.assertEqual((r2 >> 1) & ((1 << 127) - 1), cid >> 1)

        await bfm.send_command(3)
        r6 = await bfm.receive_response(48)
        self.assertEqual((r6 >> 24) & 0xffff, 0x1234)
        self.assertEqual((r6 >> 40) & 0x3f, 3)

        await bfm.send_command(9, 0x1234_0000)
        r2 = await bfm.receive_response(136)
        self.assertEqual((r2 >> 1) & ((1 << 127) - 1), csd >> 1)

        await bfm.send_command(7, 0x1234_0000)
        bfm.assert_r1(await bfm.receive_response(48), 7)
        await bfm.send_command(16, BLOCK_SIZE)
        bfm.assert_r1(await bfm.receive_response(48), 16)

        # Sharp MAINTE reads both SD-specific register blocks while opening a
        # drive. ACMD13 is required even though its all-zero status fields are
        # sufficient for this simple, unpartitioned card model.
        await bfm.send_command(55, 0x1234_0000)
        bfm.assert_r1(await bfm.receive_response(48), 55)
        await bfm.send_command(51)
        bfm.assert_r1(await bfm.receive_response(48), 51)
        await bfm.receive_data_block(build_scr().to_bytes(8, "big"))

        await bfm.send_command(55, 0x1234_0000)
        bfm.assert_r1(await bfm.receive_response(48), 55)
        await bfm.send_command(13)
        bfm.assert_r1(await bfm.receive_response(48), 13)
        await bfm.receive_data_block(bytes(64))

    async def _receive_sim_block(self, applet, bfm, expected, *, timeout_cycles=2048):
        async def diagnostic():
            pipe = applet.sd_card_iface._pipe
            agent = self._active_host_agent
            diagnostics = decode_fpga_diagnostics(
                await applet.sd_card_iface._diagnostics_snapshot)
            return (
                f"host readable={pipe.readable}, "
                f"flush readable={pipe.flush_readable}, host requests="
                f"{agent.requests_started}/{agent.requests_completed}, "
                f"transaction={diagnostics['host_transaction_phase']}, "
                f"raw-count={diagnostics['host_reply_payload_bytes']}, "
                f"banks={diagnostics['read_bank_states']:#x}, "
                f"errors={await applet.sd_card_iface.protocol_errors}, "
                f"prefetch waits={await applet.sd_card_iface.prefetch_waits}")

        await bfm.receive_data_block(
            expected, timeout_cycles=timeout_cycles, timeout_diagnostic=diagnostic)

    async def _complete_sim_busy_request(self, applet, bfm, description):
        saw_busy = False
        for _ in range(4096):
            _, _, data_sample = await bfm.cycle()
            if data_sample[0] == (1, 0):
                saw_busy = True
            if saw_busy and data_sample[0][0] == 0:
                return
        pipe = applet.sd_card_iface._pipe
        agent = self._active_host_agent
        self.fail(
            f"timed out waiting for {description} (host readable={pipe.readable}, "
            f"flush readable={pipe.flush_readable}, host requests="
            f"{agent.requests_started}/{agent.requests_completed}, "
            f"errors={await applet.sd_card_iface.protocol_errors})")

    async def _prefetch_second_block_then_stop(self, applet, bfm, first_block):
        await bfm.send_command(18, 0)
        bfm.assert_r1(await bfm.receive_response(48), 18)
        await self._receive_sim_block(applet, bfm, first_block)
        await bfm.send_command(12)
        bfm.assert_r1(await bfm.receive_response(48), 12)
        await bfm.idle(16)

    async def _assert_cmd13_reports_persistent_error(self, bfm):
        await bfm.send_command(13, 0x1234_0000)
        response = await bfm.receive_response(48)
        bfm.assert_r1(response, 13)
        status = (response >> 8) & 0xffff_ffff
        self.assertNotEqual(
            status & R1_PERSISTENT_ERROR_MASK, 0,
            f"CMD13 returned clean status {status:#010x} after a backend mutation failure")

    async def _receive_optional_sim_block(self, bfm, *, timeout_cycles=2048):
        for _ in range(timeout_cycles):
            _, _, data_sample = await bfm.cycle()
            if data_sample[0] == (1, 0):
                break
        else:
            return None

        block = 0
        for _ in range(BLOCK_SIZE * 8):
            _, _, data_sample = await bfm.cycle()
            if data_sample[0][0] != 1:
                return None
            block = (block << 1) | data_sample[0][1]
        block = block.to_bytes(BLOCK_SIZE, "big")

        received_crc = 0
        for _ in range(16):
            _, _, data_sample = await bfm.cycle()
            if data_sample[0][0] != 1:
                return None
            received_crc = (received_crc << 1) | data_sample[0][1]

        _, _, data_sample = await bfm.cycle()
        return block, received_crc == crc16(block), data_sample[0] == (1, 1)

    def _run_simulation(self, payload, scenario, *, setup_arguments=(), host_stalls=(),
            embed_boot_prefix=False, worker_factory=InlineWorker):
        with tempfile.NamedTemporaryFile() as image:
            image.write(payload)
            image.flush()

            build_arguments = (
                ("--boot-prefix", image.name) if embed_boot_prefix else ())
            args = self._parse_args([
                *build_arguments, *setup_arguments, image.name])
            assembly = SimulationAssembly()
            applet = self.applet_cls(assembly)
            applet.build(args)
            applet.sd_card_iface._worker_factory = worker_factory

            clk = assembly.get_pin("A2")
            cmd = assembly.get_pin("A5")
            dat = [assembly.get_pin(name) for name in ("A1", "A0", "A7", "A6")]
            host_agent = SimHostServiceAgent(
                applet.sd_card_iface, clk, stalls=host_stalls)
            self._active_host_agent = host_agent
            state = {"ready": False, "done": False, "error": None}

            async def card_host(ctx):
                while not state["ready"]:
                    await ctx.tick()
                bfm = SDHostBFM(ctx, self, clk=clk, cmd=cmd, dat=dat)
                try:
                    await self._initialize_sim_card(
                        applet, bfm, image, payload, transfer_speed=args.transfer_speed)
                    await scenario(applet, bfm, image)
                except BaseException as error:
                    state["error"] = error
                finally:
                    state["done"] = True
                    # Wake the foreground host agent if the scenario ended between requests.
                    await bfm.cycle()

            async def main(ctx):
                await applet.setup(args)
                state["ready"] = True
                try:
                    await host_agent.run(ctx, stopped=lambda: state["done"])
                finally:
                    await applet.sd_card_iface.close()
                if state["error"] is not None:
                    raise state["error"]

            assembly.add_testbench(card_host, background=True)
            try:
                assembly.run(main)
            finally:
                del self._active_host_agent

    def test_sim_initialization_and_single_block_read(self):
        payload = bytes(
            ((index // BLOCK_SIZE) * 0x31 + index) & 0xff
            for index in range(BLOCK_SIZE * 8))

        async def scenario(applet, bfm, image):
            await bfm.send_command(17, BLOCK_SIZE)
            bfm.assert_r1(await bfm.receive_response(48), 17)
            await self._receive_sim_block(
                applet, bfm, payload[BLOCK_SIZE:BLOCK_SIZE * 2])

        self._run_simulation(payload, scenario)

    def test_sim_read_data_starts_after_nac_gap(self):
        payload = bytes(
            ((index // BLOCK_SIZE) * 0x61 + index) & 0xff
            for index in range(BLOCK_SIZE * 8))

        async def scenario(applet, bfm, image):
            # Use the immutable boot bank so backend service latency cannot inflate the interval
            # this test is specifically measuring.
            await bfm.send_command(17, 0)
            bfm.assert_r1(await bfm.receive_response(48), 17)

            released_cycles = 0
            while True:
                _, _, data_sample = await bfm.cycle()
                if data_sample[0] == (1, 0):
                    break
                self.assertEqual(
                    data_sample[0][0], 0,
                    "DAT0 must remain released before the read start token")
                released_cycles += 1
                self.assertLessEqual(
                    released_cycles, READ_NAC_CLOCKS + 4,
                    "read start token exceeded the programmed Nac plus the bounded "
                    "response/FSM pipeline")

            self.assertGreaterEqual(released_cycles, READ_NAC_CLOCKS)

        self._run_simulation(
            payload, scenario, setup_arguments=("--prime-first-read",),
            embed_boot_prefix=True)

    def test_sim_cmd17_wire_capture_matches_pad_observed_r1(self):
        payload = bytes(
            ((index // BLOCK_SIZE) * 0x71 + index) & 0xff
            for index in range(BLOCK_SIZE * 8))

        async def scenario(applet, bfm, image):
            await bfm.send_command(17, 0)
            response = await bfm.receive_response(48)
            bfm.assert_r1(response, 17)
            captured = (
                (await applet.sd_card_iface.command_low_count) << 32 |
                await applet.sd_card_iface.clock_count)
            self.assertEqual(captured & ((1 << 48) - 1), response)

        self._run_simulation(
            payload, scenario,
            setup_arguments=("--prime-first-read", "--capture-cmd17-wire"),
            embed_boot_prefix=True)

    def test_sim_cmd55_predrives_idle_before_aligned_response(self):
        payload = bytes(
            ((index // BLOCK_SIZE) * 0x47 + index) & 0xff
            for index in range(BLOCK_SIZE * 8))

        async def scenario(applet, bfm, image):
            await bfm.send_command(55, 0x1234_0000)

            # MAINTE's first command after raising the PXA MMC clock is CMD55. The card must
            # already own CMD at idle high before it drives the response start low, so the
            # external level translator does not consume part of the start bit while turning
            # around.
            self.assertEqual(bfm.context.get(bfm.cmd.oe), 1)
            self.assertEqual(bfm.context.get(bfm.cmd.o), 1)
            for _ in range(RESPONSE_GUARD_CLOCKS + 1):
                command_oe, command_o, _ = await bfm.cycle()
                self.assertEqual((command_oe, command_o), (1, 1))

            response = await bfm.receive_response(48)
            bfm.assert_r1(response, 55)
            self.assertEqual((response >> 8) & 0xffff_ffff, (4 << 9) | (1 << 8) | (1 << 5))

        self._run_simulation(payload, scenario)

    def test_sim_rearm_starts_fresh_card_session(self):
        payload = bytearray(
            ((index // BLOCK_SIZE) * 0x53 + index) & 0xff
            for index in range(BLOCK_SIZE * 8))
        partition_lba = 4
        partition_entry = 446
        payload[partition_entry + 4] = 0x06
        payload[partition_entry + 8:partition_entry + 12] = \
            partition_lba.to_bytes(4, "little")
        payload[partition_entry + 12:partition_entry + 16] = \
            (len(payload) // BLOCK_SIZE - partition_lba).to_bytes(4, "little")
        payload[510:512] = b"\x55\xaa"
        payload = bytes(payload)

        async def scenario(applet, bfm, image):
            await bfm.send_command(17, BLOCK_SIZE * 3)
            bfm.assert_r1(await bfm.receive_response(48), 17)
            await self._receive_sim_block(
                applet, bfm, payload[BLOCK_SIZE * 3:BLOCK_SIZE * 4])

            # Restore the source CID that _initialize_sim_card deliberately changed after the
            # first coherent snapshot, then emulate a target rail fall. Rearming must reset both
            # sides of the async transport while retaining the same host backing session.
            stat = os.fstat(image.fileno())
            await applet.sd_card_iface._cid.set(
                build_cid(serial=(stat.st_ino ^ stat.st_size) & 0xffff_ffff))
            generation = applet.sd_card_iface._generation

            async def hold_reset():
                # Simulation register writes are instantaneous, whereas physical USB writes
                # leave enabled low for many Glasgow sync clocks. Advance the model while reset
                # is asserted so both sides of every transport FIFO observe the lifecycle.
                # Amaranth's async-FIFO model requires destination-domain edges to settle reset;
                # the separate host regression covers a physically stopped external SD clock.
                await bfm.idle(8)

            await applet.sd_card_iface.rearm(
                reason="simulation target-power cycle", reset_hold=0,
                after_disable=hold_reset)
            self.assertTrue(applet.sd_card_iface._active)
            self.assertNotEqual(applet.sd_card_iface._generation, generation)

            # MAINTE does not provide the long pre-command idle sequence required by modern
            # interpretations of the SD specification. Do not add any post-reset padding here;
            # _initialize_sim_card's four clocks are the entire available preamble.
            await self._initialize_sim_card(
                applet, bfm, image, payload, transfer_speed=25_000_000)
            host_requests_before_boot_read = self._active_host_agent.requests_started
            self._active_host_agent.stalls.append(100_000)
            await bfm.send_command(17, 0)
            bfm.assert_r1(await bfm.receive_response(48), 17)
            await self._receive_sim_block(applet, bfm, payload[:BLOCK_SIZE])
            await bfm.send_command(17, partition_lba * BLOCK_SIZE)
            bfm.assert_r1(await bfm.receive_response(48), 17)
            await self._receive_sim_block(
                applet, bfm,
                payload[partition_lba * BLOCK_SIZE:(partition_lba + 1) * BLOCK_SIZE])
            self.assertEqual(
                self._active_host_agent.requests_started,
                host_requests_before_boot_read,
                "embedded boot sectors must not wait for host service")

        self._run_simulation(
            payload, scenario, setup_arguments=("--prime-first-read",),
            embed_boot_prefix=True)

    def test_sim_single_and_multi_bit_writes_and_erase(self):
        payload = bytes(
            ((index // BLOCK_SIZE) * 0x31 + index) & 0xff
            for index in range(BLOCK_SIZE * 8))

        async def scenario(applet, bfm, image):
            one_bit_address = BLOCK_SIZE * 2
            one_bit_block = bytes((0x5a ^ index) & 0xff for index in range(BLOCK_SIZE))
            await bfm.send_command(24, one_bit_address)
            bfm.assert_r1(await bfm.receive_response(48), 24)
            await bfm.send_data_block_1bit(one_bit_block)
            self.assertEqual(await bfm.receive_data_response(), [0, 0, 1, 0, 1])
            await self._complete_sim_busy_request(
                applet, bfm, "host-backed 1-bit write completion")
            self.assertEqual(
                os.pread(image.fileno(), BLOCK_SIZE, one_bit_address), one_bit_block)

            await bfm.send_command(55)
            bfm.assert_r1(await bfm.receive_response(48), 55)
            await bfm.send_command(6, 2)
            bfm.assert_r1(await bfm.receive_response(48), 6)

            write_address = BLOCK_SIZE * 3
            write_block = bytes((0xa5 ^ index) & 0xff for index in range(BLOCK_SIZE))
            await bfm.send_command(24, write_address)
            bfm.assert_r1(await bfm.receive_response(48), 24)
            await bfm.send_data_block_4bit(write_block)
            self.assertEqual(await bfm.receive_data_response(), [0, 0, 1, 0, 1])
            await self._complete_sim_busy_request(
                applet, bfm, "host-backed 4-bit write completion")
            self.assertEqual(os.pread(image.fileno(), BLOCK_SIZE, write_address), write_block)

            rejected_address = 0
            rejected_block = bytes([0x3c]) * BLOCK_SIZE
            original_block = os.pread(image.fileno(), BLOCK_SIZE, rejected_address)
            writes_before = await applet.sd_card_iface.write_count
            await bfm.send_command(24, rejected_address)
            bfm.assert_r1(await bfm.receive_response(48), 24)
            await bfm.send_data_block_4bit(rejected_block, valid_crc=False)
            self.assertEqual(await bfm.receive_data_response(), [0, 1, 0, 1, 1])
            await bfm.idle(32)
            self.assertFalse(applet.sd_card_iface._pipe.flush_readable)
            self.assertEqual(await applet.sd_card_iface.write_count, writes_before)
            self.assertEqual(
                os.pread(image.fileno(), BLOCK_SIZE, rejected_address), original_block)

            erase_address = BLOCK_SIZE
            await bfm.send_command(32, erase_address)
            bfm.assert_r1(await bfm.receive_response(48), 32)
            await bfm.send_command(33, erase_address)
            bfm.assert_r1(await bfm.receive_response(48), 33)
            await bfm.send_command(38)
            bfm.assert_r1(await bfm.receive_response(48), 38)
            await self._complete_sim_busy_request(
                applet, bfm, "host-backed erase completion")
            self.assertEqual(
                os.pread(image.fileno(), BLOCK_SIZE, erase_address),
                bytes([0xff]) * BLOCK_SIZE)

        self._run_simulation(payload, scenario)

    # These regressions specify the required behavior for known P1 defects without changing the
    # implementation. Remove expectedFailure as each defect is fixed; an unexpected success keeps
    # the suite red until that cleanup is made deliberately.
    @unittest.expectedFailure
    def test_p1_backend_read_error_is_not_returned_as_valid_ff_block(self):
        """A backend failure must not appear on the SD wire as a valid all-FF sector."""
        payload = bytes(
            ((index // BLOCK_SIZE) * 0x29 + index) & 0xff
            for index in range(BLOCK_SIZE * 8))
        failed_address = BLOCK_SIZE * 4

        class FailingReadWorker(InlineWorker):
            async def read_block(self, address):
                if address == failed_address:
                    raise BackendError(BackendStatus.IOError, "injected read failure")
                return await super().read_block(address)

        async def scenario(applet, bfm, image):
            await bfm.send_command(17, failed_address)
            bfm.assert_r1(await bfm.receive_response(48), 17)
            received = await self._receive_optional_sim_block(bfm)
            valid_ff_block = received is not None and \
                received[0] == bytes([0xff]) * BLOCK_SIZE and received[1:] == (True, True)
            self.assertFalse(
                valid_ff_block,
                "backend failure was returned as a CRC-valid all-FF sector")

        self._run_simulation(payload, scenario, worker_factory=FailingReadWorker)

    @unittest.expectedFailure
    def test_p1_cached_block_is_invalidated_after_write(self):
        """A successful write must supersede any prefetched copy of the same sector."""
        payload = bytes(
            ((index // BLOCK_SIZE) * 0x31 + index) & 0xff
            for index in range(BLOCK_SIZE * 8))
        replacement = bytes((0xa5 ^ index) & 0xff for index in range(BLOCK_SIZE))

        async def scenario(applet, bfm, image):
            await self._prefetch_second_block_then_stop(
                applet, bfm, payload[:BLOCK_SIZE])

            await bfm.send_command(24, BLOCK_SIZE)
            bfm.assert_r1(await bfm.receive_response(48), 24)
            await bfm.send_data_block_1bit(replacement)
            self.assertEqual(await bfm.receive_data_response(), [0, 0, 1, 0, 1])
            await self._complete_sim_busy_request(
                applet, bfm, "host-backed cache-coherency write")
            self.assertEqual(
                os.pread(image.fileno(), BLOCK_SIZE, BLOCK_SIZE), replacement)

            await bfm.send_command(17, BLOCK_SIZE)
            bfm.assert_r1(await bfm.receive_response(48), 17)
            await self._receive_sim_block(applet, bfm, replacement)

        self._run_simulation(payload, scenario)

    @unittest.expectedFailure
    def test_p1_cached_block_is_invalidated_after_erase(self):
        """A successful erase must supersede any prefetched copy of the erased sector."""
        payload = bytes(
            ((index // BLOCK_SIZE) * 0x53 + index) & 0xff
            for index in range(BLOCK_SIZE * 8))
        erased = bytes([0xff]) * BLOCK_SIZE

        async def scenario(applet, bfm, image):
            await self._prefetch_second_block_then_stop(
                applet, bfm, payload[:BLOCK_SIZE])

            await bfm.send_command(32, BLOCK_SIZE)
            bfm.assert_r1(await bfm.receive_response(48), 32)
            await bfm.send_command(33, BLOCK_SIZE)
            bfm.assert_r1(await bfm.receive_response(48), 33)
            await bfm.send_command(38)
            bfm.assert_r1(await bfm.receive_response(48), 38)
            await self._complete_sim_busy_request(
                applet, bfm, "host-backed cache-coherency erase")
            self.assertEqual(
                os.pread(image.fileno(), BLOCK_SIZE, BLOCK_SIZE), erased)

            await bfm.send_command(17, BLOCK_SIZE)
            bfm.assert_r1(await bfm.receive_response(48), 17)
            await self._receive_sim_block(applet, bfm, erased)

        self._run_simulation(payload, scenario)

    @unittest.expectedFailure
    def test_p1_failed_write_is_reported_by_cmd13(self):
        """A backend write failure must remain visible after DAT0 busy is released."""
        payload = bytes(
            ((index // BLOCK_SIZE) * 0x67 + index) & 0xff
            for index in range(BLOCK_SIZE * 8))
        replacement = bytes((0x3c ^ index) & 0xff for index in range(BLOCK_SIZE))

        class FailingWriteWorker(InlineWorker):
            async def write_block(self, address, block):
                raise BackendError(BackendStatus.IOError, "injected write failure")

        async def scenario(applet, bfm, image):
            address = BLOCK_SIZE * 2
            original = os.pread(image.fileno(), BLOCK_SIZE, address)
            await bfm.send_command(24, address)
            bfm.assert_r1(await bfm.receive_response(48), 24)
            await bfm.send_data_block_1bit(replacement)
            self.assertEqual(await bfm.receive_data_response(), [0, 0, 1, 0, 1])
            await self._complete_sim_busy_request(
                applet, bfm, "injected failed host write")
            self.assertEqual(os.pread(image.fileno(), BLOCK_SIZE, address), original)
            await self._assert_cmd13_reports_persistent_error(bfm)

        self._run_simulation(payload, scenario, worker_factory=FailingWriteWorker)

    @unittest.expectedFailure
    def test_p1_failed_erase_is_reported_by_cmd13(self):
        """A backend erase failure must remain visible after DAT0 busy is released."""
        payload = bytes(
            ((index // BLOCK_SIZE) * 0x79 + index) & 0xff
            for index in range(BLOCK_SIZE * 8))

        class FailingEraseWorker(InlineWorker):
            async def erase(self, start, end):
                raise BackendError(BackendStatus.IOError, "injected erase failure")

        async def scenario(applet, bfm, image):
            address = BLOCK_SIZE * 2
            original = os.pread(image.fileno(), BLOCK_SIZE, address)
            await bfm.send_command(32, address)
            bfm.assert_r1(await bfm.receive_response(48), 32)
            await bfm.send_command(33, address)
            bfm.assert_r1(await bfm.receive_response(48), 33)
            await bfm.send_command(38)
            bfm.assert_r1(await bfm.receive_response(48), 38)
            await self._complete_sim_busy_request(
                applet, bfm, "injected failed host erase")
            self.assertEqual(os.pread(image.fileno(), BLOCK_SIZE, address), original)
            await self._assert_cmd13_reports_persistent_error(bfm)

        self._run_simulation(payload, scenario, worker_factory=FailingEraseWorker)

    def test_sim_sustained_cmd18_with_host_stalls_and_random_cmd12(self):
        payload = b"".join(
            bytes(((block * 0x31) + index) & 0xff for index in range(BLOCK_SIZE))
            for block in range(48))
        rng = random.Random(0x18_12_860)
        stalls = [0, 5000] + [rng.randrange(0, 5200) for _ in range(32)]

        async def scenario(applet, bfm, image):
            for round_index in range(3):
                first_block = round_index * 12
                transferred_blocks = 4 + rng.randrange(3)
                await bfm.send_command(18, first_block * BLOCK_SIZE)
                bfm.assert_r1(await bfm.receive_response(48), 18)
                for block in range(first_block, first_block + transferred_blocks):
                    await self._receive_sim_block(
                        applet, bfm,
                        payload[block * BLOCK_SIZE:(block + 1) * BLOCK_SIZE],
                        timeout_cycles=8192)

                # Vary CMD12 from immediately after a complete block to well into the following
                # block. The pending prefetch may still be Filling, already Ready, or Active.
                await bfm.idle(rng.randrange(512))
                await bfm.send_command(12)
                bfm.assert_r1(await bfm.receive_response(48), 12)
                await bfm.idle(BLOCK_SIZE * 8 + 64)

                guard_block = 40 + round_index
                await bfm.send_command(17, guard_block * BLOCK_SIZE)
                bfm.assert_r1(await bfm.receive_response(48), 17)
                await self._receive_sim_block(
                    applet, bfm,
                    payload[guard_block * BLOCK_SIZE:(guard_block + 1) * BLOCK_SIZE],
                    timeout_cycles=8192)

            # Every raw reply is one USB packet with an explicit expected address. Random
            # CMD12/CMD17 changes exercise the mismatch drain and must not relabel prefetched data.
            self.assertGreater(await applet.sd_card_iface.prefetch_waits, 0)
            self.assertEqual(await applet.sd_card_iface.buffer_faults, 0)
            self.assertEqual(await applet.sd_card_iface.protocol_errors, 0)
            self.assertEqual(await applet.sd_card_iface.stale_replies, 0)

        self._run_simulation(payload, scenario, host_stalls=stalls)
