import asyncio

from glasgow.applet import GlasgowAppletV2TestCase, synthesis_test

from . import (
    BLOCK_SIZE, DEFAULT_TRANSFER_SPEED, SDCardApplet,
    build_cid, build_csd, build_scr, crc7, crc16,
    decode_csd_transfer_speed, encode_csd_transfer_speed, parse_transfer_speed,
)
from .test_sim import SDCardSimulationTestMixin


class SDCardAppletTestCase(
        SDCardSimulationTestMixin, GlasgowAppletV2TestCase, applet=SDCardApplet):
    @synthesis_test
    def test_build(self):
        self.assertBuilds(revision="C3")

    def test_crc_vectors(self):
        self.assertEqual(crc7(0x4000_0000_00, 40), 0x4a)  # CMD0
        self.assertEqual(crc7(0x5100_0000_00, 40), 0x2a)  # CMD17, address 0
        self.assertEqual(crc16(bytes([0xff]) * BLOCK_SIZE), 0x7fa1)

    def test_register_crcs(self):
        for register in (build_cid(), build_csd(16 * 1024 * 1024)):
            self.assertEqual((register >> 1) & 0x7f, crc7(register >> 8, 120))
            self.assertEqual(register & 1, 1)

    def test_read_only_csd(self):
        self.assertEqual((build_csd(16 * 1024 * 1024) >> 12) & 1, 0)
        self.assertEqual((build_csd(16 * 1024 * 1024, read_only=True) >> 12) & 1, 1)

    def test_csd_transfer_speed_encoding(self):
        default = build_csd(16 * 1024 * 1024)
        intermediate = build_csd(16 * 1024 * 1024, transfer_speed=12_500_000)
        conservative = build_csd(16 * 1024 * 1024, transfer_speed=5_000_000)
        self.assertEqual((default >> 96) & 0xff, 0x32)
        self.assertEqual((intermediate >> 96) & 0xff, 0x12)
        self.assertEqual(decode_csd_transfer_speed((intermediate >> 96) & 0xff), 12_000_000)
        self.assertEqual((conservative >> 96) & 0xff, 0x59)
        self.assertEqual(encode_csd_transfer_speed(5_000_000), 0x59)
        self.assertEqual((conservative >> 1) & 0x7f, crc7(conservative >> 8, 120))

    def test_transfer_speed_argument(self):
        self.assertEqual(parse_transfer_speed("25m"), DEFAULT_TRANSFER_SPEED)
        self.assertEqual(parse_transfer_speed("12.5 Mbit/s"), 12_500_000)
        self.assertEqual(self._parse_args(["--transfer-speed", "5m", "card.img"]).transfer_speed,
            5_000_000)

    def test_power_idle_high_z_argument(self):
        args = self._parse_args(["--power-idle-high-z", "-V", "B=3.3", "card.img"])
        self.assertTrue(args.power_idle_high_z)
        self.assertIsNone(args.power_off_threshold)
        self.assertIsNone(args.power_on_threshold)
        self.assertIsNone(args.power_poll_interval)
        self.assertIsNone(args.power_debounce_samples)
        self.assertEqual(args.power_resume_settle, 0.005)
        self.assertEqual(args.power_rearm_min_interval, 10.0)
        self.assertEqual(args.power_rearm_reset_hold, 0.05)
        self.assertFalse(args.trace_commands)
        self.assertFalse(args.capture_cmd17_wire)
        self.assertIsNone(args.preinit_trace)
        self.assertEqual(args.preinit_trace_interval, 0.25)
        self.assertTrue(self._parse_args([
            "--trace-commands", "--clk", "B2", "card.img",
        ]).trace_commands)
        self.assertTrue(self._parse_args([
            "--capture-cmd17-wire", "--clk", "B2", "card.img",
        ]).capture_cmd17_wire)

    def test_high_z_setup_clears_voltage_alert_before_arming_signals(self):
        events = []

        class Device:
            async def reset_alert(self, port):
                events.append(("reset-alert", port))

            async def poll_alert(self):
                events.append(("poll-alert",))

            async def measure_voltage(self, port):
                events.append(("measure", port))
                return 0.0

        class Assembly:
            device = Device()

        class Interface:
            async def set_signal_pulls(self, device, port, *, enabled):
                events.append(("pulls", str(port), enabled))

            async def configure(self, image, **kwargs):
                events.append(("configure", image))

        async def scenario():
            args = self._parse_args([
                "--power-idle-high-z", "-V", "B=3.3",
                "--clk", "B2", "--cmd", "B5", "--dat", "B1,B0,B7,B6",
                "card.img",
            ])
            applet = SDCardApplet(Assembly())
            applet._sd_port = args.clk.port
            applet.sd_card_iface = Interface()
            await applet.setup(args)

        asyncio.run(scenario())
        self.assertEqual(events, [
            ("reset-alert", "B"),
            ("poll-alert",),
            ("pulls", "B", False),
            ("measure", "B"),
            ("configure", "card.img"),
        ])

    def test_power_idle_high_z_default(self):
        fixed = self._parse_args(["-V", "B=3.3", "--clk", "B2", "card.img"])
        sensed = self._parse_args(["-V", "B=SB", "--clk", "B2", "card.img"])
        legacy = self._parse_args(
            ["-V", "B=3.3", "--clk", "B2", "--no-power-monitor", "card.img"])
        explicit_off = self._parse_args(
            ["-V", "B=3.3", "--clk", "B2", "--no-power-idle-high-z", "card.img"])
        self.assertIsNone(fixed.power_idle_high_z)
        self.assertTrue(SDCardApplet._resolve_power_idle_high_z(fixed))
        self.assertFalse(SDCardApplet._resolve_power_idle_high_z(sensed))
        self.assertFalse(SDCardApplet._resolve_power_idle_high_z(legacy))
        self.assertFalse(SDCardApplet._resolve_power_idle_high_z(explicit_off))

    def test_csd_capacity_through_1_gib(self):
        for size in (2048, 1 << 20, 16 << 20, 512 << 20, 1024 << 20):
            csd = build_csd(size)
            read_block_length = 1 << ((csd >> 80) & 0xf)
            c_size = (csd >> 62) & 0xfff
            c_size_mult = (csd >> 47) & 0x7
            capacity = (c_size + 1) * (1 << (c_size_mult + 2)) * read_block_length
            self.assertEqual(capacity, size)

    def test_scr_advertises_one_and_four_bit_modes(self):
        scr = build_scr()
        self.assertEqual((scr >> 56) & 0xf, 0)  # SD 1.0/1.01
        self.assertEqual((scr >> 48) & 0xf, 0b0101)
        self.assertEqual((build_scr(support_4bit=False) >> 48) & 0xf, 0b0001)


# Keep the applet's historical test module as the single suite entry point while the focused
# cases live in smaller implementation-specific modules.
from ._test_gateware import (
    BlockBufferTestCase, COBSRecoveryTestCase, CommandEngineErrorBreakdownTestCase,
    ReadBufferManagerTestCase,
)
from ._test_host import (
    BackendWorkerTestCase, FAT16ImageTestCase, FileBlockBackendTestCase,
    SDCardInterfaceTestCase, SDCardProtocolTestCase,
)
from ._test_preinit_trace import PreinitTraceTestCase
from ._test_sideband_console import SidebandConsoleTestCase
from ._test_zaurus import ConsoleTransportTestCase, ZaurusSDControllerTestCase
