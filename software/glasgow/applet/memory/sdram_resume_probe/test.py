import unittest

from amaranth.lib import io
from amaranth.sim import Simulator

from glasgow.applet import GlasgowAppletV2TestCase, synthesis_test
from glasgow.gateware.ports import PortGroup

from . import (
    SAMPLE_ADDRESS_0,
    SAMPLE_A2, SAMPLE_A3, SAMPLE_A5, SAMPLE_A6, SAMPLE_A7,
    SAMPLE_BA0, SAMPLE_BA1, SAMPLE_CAS_N, SAMPLE_CKE, SAMPLE_CS_N,
    SAMPLE_RAS_N, SAMPLE_WE_N,
    SDRAM_CAPTURE_DEPTH, SDRAMCaptureMode, SDRAMResumeCapture,
    SDRAM_SIGNAL_PROFILES,
    SDRAMResumeProbeApplet, SDRAMResumeProbeComponent,
    SDRAMResumeProbeError, decode_capture_record, is_self_refresh_sample,
    require_request_bool, require_request_int, require_request_str,
    resolve_arm_signatures, validate_signal_profile_attestation,
)


def sample(*, cke=1, cs_n=1, ras_n=1, cas_n=1, we_n=1,
        bank=0, a2=0, a3=0, a5=0, a6=0, a7=0):
    value = 0
    for bit, state in (
            (SAMPLE_CKE, cke),
            (SAMPLE_CS_N, cs_n),
            (SAMPLE_RAS_N, ras_n),
            (SAMPLE_CAS_N, cas_n),
            (SAMPLE_WE_N, we_n),
            (SAMPLE_BA0, bank & 1),
            (SAMPLE_BA1, (bank >> 1) & 1),
            (SAMPLE_A2, a2),
            (SAMPLE_A3, a3),
            (SAMPLE_A5, a5),
            (SAMPLE_A6, a6),
            (SAMPLE_A7, a7)):
        value |= int(bool(state)) << bit
    return value


def profile_sample(profile_name, *, address_values=None, **kwargs):
    profile = SDRAM_SIGNAL_PROFILES[profile_name]
    value = sample(**kwargs)
    address_values = address_values or {}
    for index, signal in enumerate(profile.address_signals):
        value &= ~(1 << (SAMPLE_ADDRESS_0 + index))
        value |= (
            int(bool(address_values.get(signal, 0))) <<
            (SAMPLE_ADDRESS_0 + index))
    return value


DESELECT = sample()
SELF_REFRESH = sample(
    cke=0, cs_n=0, ras_n=0, cas_n=0, we_n=1)
PRECHARGE = sample(
    cs_n=0, ras_n=0, cas_n=1, we_n=0)
ZERO_ACTIVATE = sample(
    cs_n=0, ras_n=0, cas_n=1, we_n=1)
ZERO_READ = sample(
    cs_n=0, ras_n=1, cas_n=0, we_n=1)
TARGET_ACTIVATE = sample(
    cs_n=0, ras_n=0, cas_n=1, we_n=1, a5=1, a6=1)
TARGET_READ = sample(
    cs_n=0, ras_n=1, cas_n=0, we_n=1,
    a2=1, a3=1, a6=1, a7=1)


class SDRAMResumeProbeProtocolTestCase(unittest.TestCase):
    def test_signal_profiles_have_distinct_attested_mappings(self):
        v9 = SDRAM_SIGNAL_PROFILES["v9-a0018330"].metadata()
        v14 = SDRAM_SIGNAL_PROFILES["v14-a00302c4"].metadata()
        v15 = SDRAM_SIGNAL_PROFILES["v15-a0030280"].metadata()
        self.assertEqual(
            v9["address_pin_order"], ["A2", "A3", "A5", "A6", "A7"])
        self.assertEqual(
            v14["address_pin_order"], ["A0", "A4", "A5", "A6", "A7"])
        self.assertEqual(
            v14["active_row_signature_order"], ["A6", "A7"])
        self.assertEqual(
            v14["read_signature_order"], ["A0", "A4", "A5", "A7"])
        self.assertNotEqual(v9["mapping_sha256"], v14["mapping_sha256"])
        self.assertEqual(len(v14["mapping_sha256"]), 64)
        self.assertEqual(v15["prepared_target_physical"], "0xa0030280")
        self.assertEqual(v15["target_signature"], 0xcc)
        self.assertEqual(
            v15["mapping_sha256"],
            "53ed90c4e779bfc9733fc513d49ea3cdbfdc2f76575910cc7fc0ca9f2206e997",
        )
        self.assertNotEqual(v14["mapping_sha256"], v15["mapping_sha256"])

    def test_arm_signatures_default_to_attested_profile(self):
        self.assertEqual(
            resolve_arm_signatures("v14-a00302c4"),
            (0x00, 0xfc),
        )
        self.assertEqual(
            resolve_arm_signatures("v15-a0030280"),
            (0x00, 0xcc),
        )

    def test_arm_signatures_reject_cross_profile_target(self):
        with self.assertRaisesRegex(
                SDRAMResumeProbeError, "does not match profile"):
            resolve_arm_signatures(
                "v15-a0030280", target_signature=0xfc)

    def test_exact_pspr_profile_decodes_distinct_target_column(self):
        profile = "v15-a0030280"
        activate = profile_sample(
            profile,
            cs_n=0,
            ras_n=0,
            cas_n=1,
            we_n=1,
            address_values={6: 1, 7: 1},
        )
        read = profile_sample(
            profile,
            cs_n=0,
            ras_n=1,
            cas_n=0,
            we_n=1,
            address_values={5: 1, 7: 1},
        )
        activate_record = decode_capture_record(activate, profile)
        read_record = decode_capture_record(read, profile)
        self.assertEqual(
            activate_record["active_row_signals"], ["A6", "A7"])
        self.assertEqual(read_record["signals"]["a0"], 0)
        self.assertEqual(read_record["signals"]["a4"], 0)
        self.assertEqual(read_record["signals"]["a5"], 1)
        self.assertEqual(read_record["signals"]["a7"], 1)

    def test_arm_profile_attestation_fails_closed(self):
        v14 = SDRAM_SIGNAL_PROFILES["v14-a00302c4"].metadata()
        validate_signal_profile_attestation(
            "v14-a00302c4", v14["name"], v14["mapping_sha256"])
        with self.assertRaisesRegex(
                SDRAMResumeProbeError, "does not match built profile"):
            validate_signal_profile_attestation(
                "v14-a00302c4",
                "v9-a0018330",
                v14["mapping_sha256"])
        with self.assertRaisesRegex(
                SDRAMResumeProbeError, "SHA-256"):
            validate_signal_profile_attestation(
                "v14-a00302c4",
                v14["name"],
                "0" * 64)

    def test_attestations_require_literal_json_booleans(self):
        self.assertTrue(require_request_bool(
            {"wiring_attested": True}, "wiring_attested"))
        self.assertFalse(require_request_bool(
            {}, "wiring_attested", default=False))
        for invalid in ("true", "false", 0, 1, None, [], {}):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(
                        SDRAMResumeProbeError, "JSON boolean"):
                    require_request_bool(
                        {"wiring_attested": invalid},
                        "wiring_attested",
                    )

    def test_numeric_fields_require_literal_json_integers(self):
        self.assertEqual(
            require_request_int({"generation": 7}, "generation"), 7)
        self.assertEqual(
            require_request_int({}, "minimum_zero_reads", default=8), 8)
        for invalid in (True, False, "7", 7.0, None, [], {}):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(
                        SDRAMResumeProbeError, "JSON integer"):
                    require_request_int(
                        {"generation": invalid}, "generation")

    def test_profile_fields_require_literal_json_strings(self):
        self.assertEqual(
            require_request_str(
                {"address_profile": "v14-a00302c4"},
                "address_profile"),
            "v14-a00302c4")
        for invalid in (True, 7, 7.0, None, [], {}):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(
                        SDRAMResumeProbeError, "JSON string"):
                    require_request_str(
                        {"address_profile": invalid}, "address_profile")


class SDRAMResumeCaptureTestCase(unittest.TestCase):
    def run_capture_test(
            self, testbench, *, signal_profile="v9-a0018330"):
        dut = SDRAMResumeCapture(signal_profile=signal_profile)
        sim = Simulator(dut)
        sim.add_clock(20e-9)
        async def launch(ctx):
            await testbench(ctx, dut)
        sim.add_testbench(launch)
        sim.run()

    @staticmethod
    async def pulse(ctx, dut, value):
        ctx.set(dut.clk_i, 0)
        await ctx.delay(4e-9)
        ctx.set(dut.clk_i, 1)
        # Model the wrapper's input FFs: the core sees the previous physical
        # sample at this edge and the new sample immediately afterward.
        await ctx.delay(1e-9)
        ctx.set(dut.sample, value)
        await ctx.delay(4e-9)
        ctx.set(dut.clk_i, 0)
        await ctx.delay(4e-9)

    async def arm(self, ctx, dut, mode):
        ctx.set(dut.generation, 0x1234)
        ctx.set(dut.mode, int(mode))
        ctx.set(dut.zero_signature, 0x00)
        ctx.set(
            dut.target_signature, dut.signal_profile.target_signature)
        ctx.set(dut.minimum_zero_reads, 8)
        ctx.set(dut.armed, 1)
        for _ in range(6):
            await self.pulse(ctx, dut, DESELECT)
        await ctx.delay(80e-9)
        self.assertEqual(ctx.get(dut.armed_ack), 1)

    async def read_record(self, ctx, dut, index):
        ctx.set(dut.record_index, index)
        await ctx.tick()
        await ctx.tick()
        return decode_capture_record(ctx.get(dut.record))

    def test_last_sleep_edge_is_retained_without_later_clock(self):
        async def testbench(ctx, dut):
            await self.arm(
                ctx, dut, SDRAMCaptureMode.DEEP_SELF_REFRESH)
            self.assertEqual(ctx.get(dut.triggered), 0)

            await self.pulse(ctx, dut, SELF_REFRESH)
            # Do not issue another target clock. The trigger must still cross
            # into the host domain as the registered last pin sample.
            await ctx.delay(100e-9)
            self.assertEqual(ctx.get(dut.triggered), 0)
            self.assertEqual(ctx.get(dut.self_refresh_seen), 0)
            self.assertEqual(ctx.get(dut.event_count), 0)
            self.assertTrue(is_self_refresh_sample(
                ctx.get(dut.last_sample)))
            record = decode_capture_record(ctx.get(dut.last_sample))
            self.assertEqual(record["command"], "auto-refresh")
            self.assertEqual(record["signals"]["cke"], 0)
            self.assertFalse(record["active_row_valid"])

        self.run_capture_test(testbench)

    def test_pin_input_ffs_retain_final_self_refresh_sample(self):
        ports = PortGroup()
        ports.clk = io.SimulationPort("i", 1, name="clk")
        ports.cke = io.SimulationPort("i", 1, name="cke")
        ports.cs = io.SimulationPort("i", 1, name="cs")
        ports.ras = io.SimulationPort("i", 1, name="ras")
        ports.cas = io.SimulationPort("i", 1, name="cas")
        ports.we = io.SimulationPort("i", 1, name="we")
        ports.ba = io.SimulationPort("i", 2, name="ba")
        ports.address = io.SimulationPort("i", 5, name="address")
        dut = SDRAMResumeProbeComponent(ports)

        async def drive_physical_sample(ctx, value):
            ctx.set(ports.cke.i, (value >> SAMPLE_CKE) & 1)
            ctx.set(ports.cs.i, (value >> SAMPLE_CS_N) & 1)
            ctx.set(ports.ras.i, (value >> SAMPLE_RAS_N) & 1)
            ctx.set(ports.cas.i, (value >> SAMPLE_CAS_N) & 1)
            ctx.set(ports.we.i, (value >> SAMPLE_WE_N) & 1)
            ctx.set(
                ports.ba.i,
                ((value >> SAMPLE_BA0) & 1) |
                (((value >> SAMPLE_BA1) & 1) << 1))
            ctx.set(
                ports.address.i,
                ((value >> SAMPLE_A2) & 1) |
                (((value >> SAMPLE_A3) & 1) << 1) |
                (((value >> SAMPLE_A5) & 1) << 2) |
                (((value >> SAMPLE_A6) & 1) << 3) |
                (((value >> SAMPLE_A7) & 1) << 4))

        async def pulse(ctx, value):
            await drive_physical_sample(ctx, value)
            ctx.set(ports.clk.i, 0)
            await ctx.delay(4e-9)
            ctx.set(ports.clk.i, 1)
            await ctx.delay(4e-9)
            ctx.set(ports.clk.i, 0)
            await ctx.delay(4e-9)

        async def testbench(ctx):
            ctx.set(dut.generation, 1)
            ctx.set(dut.mode, SDRAMCaptureMode.DEEP_SELF_REFRESH)
            ctx.set(dut.zero_signature, 0)
            ctx.set(dut.target_signature, 0xfc)
            ctx.set(dut.minimum_zero_reads, 8)
            ctx.set(dut.armed, 1)
            for _ in range(6):
                await pulse(ctx, DESELECT)
            await ctx.delay(80e-9)
            self.assertEqual(ctx.get(dut.armed_ack), 1)

            await pulse(ctx, SELF_REFRESH)
            # No more observed clock edges occur. The pin-local input FF must
            # nevertheless retain the exact physical sample for sync-domain
            # readback.
            await ctx.delay(100e-9)
            self.assertEqual(ctx.get(dut.self_refresh_seen), 0)
            self.assertTrue(is_self_refresh_sample(
                ctx.get(dut.last_sample)))
            self.assertEqual(ctx.get(dut.event_count), 0)

        sim = Simulator(dut)
        sim.add_clock(20e-9)
        sim.add_testbench(testbench)
        sim.run()

    def test_nor_boundary_then_target_freezes(self):
        async def testbench(ctx, dut):
            await self.arm(
                ctx, dut, SDRAMCaptureMode.DEEP_SELF_REFRESH)
            await self.pulse(ctx, dut, SELF_REFRESH)
            await ctx.delay(80e-9)

            # First resumed clock exits self-refresh with a deselect/NOP.
            await self.pulse(ctx, dut, DESELECT)
            await self.pulse(ctx, dut, ZERO_ACTIVATE)
            for _ in range(8):
                await self.pulse(ctx, dut, ZERO_READ)
            await self.pulse(ctx, dut, PRECHARGE)
            await self.pulse(ctx, dut, TARGET_ACTIVATE)
            await self.pulse(ctx, dut, TARGET_READ)
            for _ in range(3):
                await self.pulse(ctx, dut, DESELECT)
            await ctx.delay(120e-9)

            self.assertEqual(ctx.get(dut.zero_reads), 8)
            self.assertEqual(ctx.get(dut.target_reads), 1)
            self.assertEqual(
                ctx.get(dut.target_reads_before_boundary), 0)
            self.assertEqual(ctx.get(dut.target_seen), 1)
            self.assertEqual(ctx.get(dut.frozen), 1)
            self.assertEqual(ctx.get(dut.overflow), 0)
            self.assertEqual(ctx.get(dut.event_count), 12)

            first = await self.read_record(ctx, dut, 0)
            last = await self.read_record(ctx, dut, 11)
            self.assertEqual(first["command"], "activate")
            self.assertEqual(last["command"], "read")
            self.assertEqual(last["active_row_discriminator"], 0b11)
            self.assertTrue(last["active_row_valid"])
            self.assertGreater(last["clock_index"], first["clock_index"])

            # Frozen capture is immutable even if the bus keeps running.
            await self.pulse(ctx, dut, PRECHARGE)
            await self.pulse(ctx, dut, ZERO_ACTIVATE)
            await self.pulse(ctx, dut, ZERO_READ)
            await ctx.delay(80e-9)
            self.assertEqual(ctx.get(dut.event_count), 12)
            self.assertEqual(ctx.get(dut.zero_reads), 8)

        self.run_capture_test(testbench)

    def test_target_before_boundary_is_not_credited(self):
        async def testbench(ctx, dut):
            await self.arm(
                ctx, dut, SDRAMCaptureMode.DEEP_SELF_REFRESH)
            await self.pulse(ctx, dut, SELF_REFRESH)
            await self.pulse(ctx, dut, DESELECT)
            await self.pulse(ctx, dut, ZERO_ACTIVATE)
            for _ in range(7):
                await self.pulse(ctx, dut, ZERO_READ)
            await self.pulse(ctx, dut, PRECHARGE)
            await self.pulse(ctx, dut, TARGET_ACTIVATE)
            await self.pulse(ctx, dut, TARGET_READ)
            for _ in range(3):
                await self.pulse(ctx, dut, DESELECT)
            await ctx.delay(80e-9)

            self.assertEqual(ctx.get(dut.target_reads), 1)
            self.assertEqual(
                ctx.get(dut.target_reads_before_boundary), 1)
            self.assertEqual(ctx.get(dut.target_seen), 0)
            self.assertEqual(ctx.get(dut.frozen), 0)

        self.run_capture_test(testbench)

    def test_calibration_mode_filters_to_pair_reads(self):
        unrelated_activate = sample(
            cs_n=0, ras_n=0, cas_n=1, we_n=1, a5=1, a6=0)
        unrelated_read = ZERO_READ

        async def testbench(ctx, dut):
            await self.arm(
                ctx, dut, SDRAMCaptureMode.CALIBRATION_FILTERED)
            self.assertEqual(ctx.get(dut.triggered), 1)

            await self.pulse(ctx, dut, unrelated_activate)
            await self.pulse(ctx, dut, unrelated_read)
            await self.pulse(ctx, dut, PRECHARGE)
            await self.pulse(ctx, dut, ZERO_ACTIVATE)
            await self.pulse(ctx, dut, ZERO_READ)
            await self.pulse(ctx, dut, PRECHARGE)
            await self.pulse(ctx, dut, TARGET_ACTIVATE)
            await self.pulse(ctx, dut, TARGET_READ)
            for _ in range(3):
                await self.pulse(ctx, dut, DESELECT)
            await ctx.delay(100e-9)

            self.assertEqual(ctx.get(dut.event_count), 2)
            self.assertEqual(ctx.get(dut.zero_reads), 1)
            self.assertEqual(ctx.get(dut.target_reads), 1)
            self.assertEqual(ctx.get(dut.target_seen), 0)
            self.assertEqual(ctx.get(dut.frozen), 0)

            zero = await self.read_record(ctx, dut, 0)
            target = await self.read_record(ctx, dut, 1)
            self.assertEqual(zero["active_row_discriminator"], 0)
            self.assertEqual(target["active_row_discriminator"], 3)
            self.assertEqual(target["signals"]["a2"], 1)
            self.assertEqual(target["signals"]["a7"], 1)

            ctx.set(dut.freeze, 1)
            for _ in range(4):
                await self.pulse(ctx, dut, DESELECT)
            await ctx.delay(80e-9)
            self.assertEqual(ctx.get(dut.frozen), 1)

        self.run_capture_test(testbench)

    def test_v14_profile_distinguishes_exact_stock_target(self):
        profile_name = "v14-a00302c4"
        target_activate = profile_sample(
            profile_name,
            cs_n=0, ras_n=0, cas_n=1, we_n=1,
            address_values={6: 1, 7: 1})
        target_read = profile_sample(
            profile_name,
            cs_n=0, ras_n=1, cas_n=0, we_n=1,
            address_values={0: 1, 4: 1, 5: 1, 7: 1})
        zero_activate = profile_sample(
            profile_name,
            cs_n=0, ras_n=0, cas_n=1, we_n=1)
        zero_read = profile_sample(
            profile_name,
            cs_n=0, ras_n=1, cas_n=0, we_n=1)

        async def testbench(ctx, dut):
            await self.arm(
                ctx, dut, SDRAMCaptureMode.DEEP_SELF_REFRESH)
            await self.pulse(ctx, dut, SELF_REFRESH)
            await self.pulse(ctx, dut, DESELECT)
            await self.pulse(ctx, dut, zero_activate)
            for _ in range(8):
                await self.pulse(ctx, dut, zero_read)
            await self.pulse(ctx, dut, PRECHARGE)
            await self.pulse(ctx, dut, target_activate)
            await self.pulse(ctx, dut, target_read)
            for _ in range(3):
                await self.pulse(ctx, dut, DESELECT)
            await ctx.delay(120e-9)

            self.assertEqual(ctx.get(dut.zero_reads), 8)
            self.assertEqual(ctx.get(dut.target_reads), 1)
            self.assertEqual(ctx.get(dut.target_seen), 1)
            self.assertEqual(ctx.get(dut.frozen), 1)
            target = await self.read_record(
                ctx, dut, ctx.get(dut.event_count) - 1)
            target = decode_capture_record(
                target["raw"], profile_name)
            self.assertEqual(
                target["active_row_signals"], ["A6", "A7"])
            self.assertEqual(target["active_row_discriminator"], 3)
            for signal in ("a0", "a4", "a5", "a7"):
                self.assertEqual(target["signals"][signal], 1)

        self.run_capture_test(
            testbench, signal_profile=profile_name)

    def test_v15_profile_distinguishes_exact_pspr_target(self):
        profile_name = "v15-a0030280"
        target_activate = profile_sample(
            profile_name,
            cs_n=0, ras_n=0, cas_n=1, we_n=1,
            address_values={6: 1, 7: 1})
        target_read = profile_sample(
            profile_name,
            cs_n=0, ras_n=1, cas_n=0, we_n=1,
            address_values={5: 1, 7: 1})
        zero_activate = profile_sample(
            profile_name,
            cs_n=0, ras_n=0, cas_n=1, we_n=1)
        zero_read = profile_sample(
            profile_name,
            cs_n=0, ras_n=1, cas_n=0, we_n=1)

        async def testbench(ctx, dut):
            await self.arm(
                ctx, dut, SDRAMCaptureMode.DEEP_SELF_REFRESH)
            await self.pulse(ctx, dut, SELF_REFRESH)
            await self.pulse(ctx, dut, DESELECT)
            await self.pulse(ctx, dut, zero_activate)
            for _ in range(8):
                await self.pulse(ctx, dut, zero_read)
            await self.pulse(ctx, dut, PRECHARGE)
            await self.pulse(ctx, dut, target_activate)
            await self.pulse(ctx, dut, target_read)
            for _ in range(3):
                await self.pulse(ctx, dut, DESELECT)
            await ctx.delay(120e-9)

            self.assertEqual(ctx.get(dut.zero_reads), 8)
            self.assertEqual(ctx.get(dut.target_reads), 1)
            self.assertEqual(ctx.get(dut.target_seen), 1)
            self.assertEqual(ctx.get(dut.frozen), 1)
            target = await self.read_record(
                ctx, dut, ctx.get(dut.event_count) - 1)
            target = decode_capture_record(
                target["raw"], profile_name)
            self.assertEqual(
                target["active_row_signals"], ["A6", "A7"])
            self.assertEqual(target["active_row_discriminator"], 3)
            self.assertEqual(target["signals"]["a0"], 0)
            self.assertEqual(target["signals"]["a4"], 0)
            self.assertEqual(target["signals"]["a5"], 1)
            self.assertEqual(target["signals"]["a7"], 1)

        self.run_capture_test(
            testbench, signal_profile=profile_name)

    def test_full_capture_fails_closed(self):
        async def testbench(ctx, dut):
            await self.arm(ctx, dut, SDRAMCaptureMode.RAW_IMMEDIATE)
            for _ in range(SDRAM_CAPTURE_DEPTH + 4):
                await self.pulse(ctx, dut, PRECHARGE)
            await ctx.delay(100e-9)
            self.assertEqual(
                ctx.get(dut.event_count), SDRAM_CAPTURE_DEPTH)
            self.assertEqual(ctx.get(dut.overflow), 1)
            self.assertEqual(ctx.get(dut.frozen), 1)

        self.run_capture_test(testbench)


class SDRAMResumeProbeAppletTestCase(
        GlasgowAppletV2TestCase, applet=SDRAMResumeProbeApplet):
    @synthesis_test
    def test_build(self):
        self.assertBuilds([
            "-V", "A=3.3,B=3.3",
            "--signal-profile", "v14-a00302c4",
            "--clk", "A1", "--cke", "A0", "--cs", "A2",
            "--ras", "A3", "--cas", "A4", "--we", "A5",
            "--ba", "A6,A7", "--address", "B0:4",
        ], revision="C3")

    def test_arguments(self):
        args = self._parse_args([
            "-V", "A=3.3,B=3.3",
            "--signal-profile", "v14-a00302c4",
            "--control", "/tmp/zaurus-sdram-probe.sock",
        ])
        self.assertEqual(str(args.control), "/tmp/zaurus-sdram-probe.sock")
        self.assertEqual(str(args.clk), "A1")
        self.assertEqual(
            tuple(map(str, args.address)),
            ("B0", "B1", "B2", "B3", "B4"))
        self.assertEqual(args.signal_profile, "v14-a00302c4")

    def test_signal_profile_is_mandatory(self):
        with self.assertRaises(SystemExit):
            self._parse_args([
                "-V", "A=3.3,B=3.3",
                "--control", "/tmp/zaurus-sdram-probe.sock",
            ])


if __name__ == "__main__":
    unittest.main()
