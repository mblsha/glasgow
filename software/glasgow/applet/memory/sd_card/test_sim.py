from collections import deque
import os
import random
import tempfile

from glasgow.simulation.assembly import SimulationAssembly

from . import BLOCK_SIZE, build_cid, build_csd
from .bfm import SDHostBFM


class _InlineWorker:
    """Keep backend calls deterministic inside the HDL simulator."""

    def __init__(self, backend):
        self.backend = backend

    async def read_block(self, address):
        return self.backend.read_block(address)

    async def write_block(self, address, block):
        return self.backend.write_block(address, block)

    async def erase(self, start, end):
        return self.backend.erase(start, end)

    async def flush(self):
        return self.backend.flush()

    async def close(self):
        return self.backend.close()


class SimHostServiceAgent:
    """Serve framed backend requests independently of SD host bus activity."""

    def __init__(self, interface, sd_clock, *, stalls=()):
        self.interface = interface
        self.sd_clock = sd_clock
        self.stalls = deque(stalls)
        self._stall_remaining = None
        self.requests_started = 0
        self.requests_completed = 0

    async def run(self, ctx):
        self.interface._pipe.bind_simulation_context(ctx)
        while True:
            await ctx.posedge(self.sd_clock.i)
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

    async def _receive_sim_block(self, applet, bfm, expected, *, timeout_cycles=2048):
        async def diagnostic():
            pipe = applet.sd_card_iface._pipe
            return (
                f"host readable={pipe.readable}, "
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
        agent = applet._sim_host_agent
        self.fail(
            f"timed out waiting for {description} (host readable={pipe.readable}, "
            f"flush readable={pipe.flush_readable}, host requests="
            f"{agent.requests_started}/{agent.requests_completed}, "
            f"errors={await applet.sd_card_iface.protocol_errors})")

    def _run_simulation(self, payload, scenario, *, setup_arguments=(), host_stalls=()):
        with tempfile.NamedTemporaryFile() as image:
            image.write(payload)
            image.flush()

            args = self._parse_args([*setup_arguments, image.name])
            assembly = SimulationAssembly()
            applet = self.applet_cls(assembly)
            applet.build(args)
            applet.sd_card_iface._worker_factory = _InlineWorker

            clk = assembly.get_pin("A2")
            cmd = assembly.get_pin("A5")
            dat = [assembly.get_pin(name) for name in ("A1", "A0", "A7", "A6")]
            host_agent = SimHostServiceAgent(
                applet.sd_card_iface, clk, stalls=host_stalls)
            applet._sim_host_agent = host_agent
            assembly.add_testbench(host_agent.run, background=True)

            async def card_host(ctx):
                bfm = SDHostBFM(ctx, self, clk=clk, cmd=cmd, dat=dat)
                await self._initialize_sim_card(
                    applet, bfm, image, payload, transfer_speed=args.transfer_speed)
                try:
                    await scenario(applet, bfm, image)
                finally:
                    await applet.sd_card_iface.close()

            async def main(ctx):
                await applet.setup(args)
                await card_host(ctx)

            assembly.run(main)

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

            self.assertGreater(await applet.sd_card_iface.prefetch_waits, 0)
            self.assertGreater(await applet.sd_card_iface.prefetch_wait_cycles, 0)
            self.assertGreater(await applet.sd_card_iface.max_prefetch_wait, 0)
            self.assertGreater(await applet.sd_card_iface.max_host_service_latency, 0)
            self.assertEqual(await applet.sd_card_iface.buffer_faults, 0)

        self._run_simulation(payload, scenario, host_stalls=stalls)
