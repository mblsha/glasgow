import asyncio
import logging

from amaranth import *
from amaranth.lib import io
from amaranth.sim import Simulator

from glasgow.gateware.ports import PortGroup
from glasgow.gateware.stream import stream_get
from glasgow.simulation.assembly import SimulationAssembly
from ... import *
from . import UARTApplet, UARTComponent, UARTInterface


class UARTAppletTestCase(GlasgowAppletV2TestCase, applet=UARTApplet):
    def test_interface_flush_waits_for_completion_target(self):
        class FakePipe:
            def __init__(self):
                self.sent = []
                self.flush_calls = 0

            async def send(self, data):
                self.sent.append(bytes(data))

            async def flush(self):
                self.flush_calls += 1

        class FakeRegister:
            def __init__(self, parent):
                self.parent = parent

            async def get(self):
                return self.parent.tx_count

            def __await__(self):
                return self.get().__await__()

        class FakeAssembly:
            def __init__(self, tx_count_updates):
                self.tx_count = 0
                self.tx_count_updates = iter(tx_count_updates)
                self.delays = []

            async def advance_runtime(self, delay=0.0):
                self.delays.append(delay)
                self.tx_count = next(self.tx_count_updates, self.tx_count)

        iface = object.__new__(UARTInterface)
        iface._logger = logging.getLogger(__name__)
        iface._level = logging.DEBUG
        iface._assembly = assembly = FakeAssembly([1, 2])
        iface._has_cts = False
        iface._tx_target = 0
        iface._sys_clk_period = 1e-6
        iface._parity = "none"
        iface._stop_bits = 1
        iface._pipe = pipe = FakePipe()
        iface._tx_count = FakeRegister(assembly)

        async def get_baud():
            return 1200

        iface.get_baud = get_baud

        async def testbench():
            await iface.write(b"AB", flush=True)

        asyncio.run(testbench())

        self.assertEqual(pipe.sent, [b"AB"])
        self.assertEqual(pipe.flush_calls, 1)
        self.assertEqual(iface._tx_target, 2)
        self.assertEqual(len(assembly.delays), 2)
        self.assertAlmostEqual(assembly.delays[0], 2 * 10 / 1200)
        self.assertAlmostEqual(assembly.delays[1], 1 * 10 / 1200)

    @synthesis_test
    def test_build(self):
        self.assertBuilds()

    @synthesis_test
    def test_build_optional_pins(self):
        self.assertBuilds(["--rx", "-", "--tx", "-"])

    @synthesis_test
    def test_build_rts_only(self):
        self.assertBuilds(["--rts", "A2"])

    @synthesis_test
    def test_build_cts_only(self):
        self.assertBuilds(["--cts", "A2"])

    def assertBuildFails(self, args, message):
        parsed_args = self._parse_args(args, mode="build")
        assembly = SimulationAssembly()
        applet = self.applet_cls(assembly)
        with self.assertRaisesRegex(GlasgowAppletError, message):
            applet.build(parsed_args)

    def test_build_rejects_rts_without_rx(self):
        self.assertBuildFails(["--rx", "-", "--rts", "A2"], "RTS requires an RX pin")

    def test_build_rejects_cts_without_tx(self):
        self.assertBuildFails(["--tx", "-", "--cts", "A2"], "CTS requires a TX pin")

    def prepare_loopback(self, assembly):
        assembly.connect_pins("A0", "A1")

    @applet_v2_simulation_test(prepare=prepare_loopback, args="--baud 9600")
    async def test_loopback(self, applet, ctx):
        await applet.uart_iface.write(bytes([0xAA, 0x55]))
        self.assertEqual(await applet.uart_iface.read(2), bytes([0xAA, 0x55]))

    @applet_v2_simulation_test(prepare=prepare_loopback, args="--baud 9600 --parity odd")
    async def test_parity_loopback(self, applet, ctx):
        await applet.uart_iface.write(bytes([0x55, 0xAA]))
        self.assertEqual(await applet.uart_iface.read(2), bytes([0x55, 0xAA]))

    @applet_v2_simulation_test(prepare=prepare_loopback, args="--baud 9600 --stop-bits 2")
    async def test_stop_bits_loopback(self, applet, ctx):
        await applet.uart_iface.write(bytes([0x55, 0xAA]))
        self.assertEqual(await applet.uart_iface.read(2), bytes([0x55, 0xAA]))

    # This test is here mainly to test the test machinery.
    @applet_v2_hardware_test(args="-V 3.3 --baud 9600", mocks=["uart_iface"])
    async def test_loopback_hw(self, applet):
        await applet.uart_iface.write(bytes([0xAA, 0x55]))
        self.assertEqual(await applet.uart_iface.read(2), bytes([0xAA, 0x55]))

    async def send_rx_byte(self, ctx, rx_port, data, *, bit_cyc):
        ctx.set(rx_port.i, 0)
        await ctx.tick().repeat(bit_cyc)
        await ctx.tick()
        for bitno in range(8):
            ctx.set(rx_port.i, (data >> bitno) & 1)
            await ctx.tick().repeat(bit_cyc)
        ctx.set(rx_port.i, 1)
        await ctx.tick().repeat(bit_cyc)

    def test_component_rts_tracks_receive_headroom(self):
        ports = PortGroup(
            rx=io.SimulationPort("i", 1, name="rx"),
            tx=None,
            rts=io.SimulationPort("o", 1, name="rts"),
            cts=None,
        )
        dut = UARTComponent(ports, parity="none", stop_bits=1)

        async def testbench(ctx):
            ctx.set(dut.use_auto, 0)
            ctx.set(dut.manual_cyc, 4)
            ctx.set(dut.o_stream.ready, 0)
            ctx.set(ports.rx.i, 1)
            await ctx.tick().repeat(4)

            self.assertEqual(ctx.get(ports.rts.o), 0)

            for value in range(dut.RTS_DEASSERT_LEVEL):
                await self.send_rx_byte(ctx, ports.rx, value, bit_cyc=4)
            await ctx.tick().repeat(4)

            self.assertEqual(ctx.get(ports.rts.o), 1)

            ctx.set(dut.o_stream.ready, 1)
            for expected in range(dut.RTS_DEASSERT_LEVEL - dut.RTS_ASSERT_LEVEL):
                self.assertEqual(await stream_get(ctx, dut.o_stream), expected)
            await ctx.tick().repeat(2)

            self.assertEqual(ctx.get(ports.rts.o), 0)

            for expected in range(dut.RTS_DEASSERT_LEVEL - dut.RTS_ASSERT_LEVEL,
                                  dut.RTS_DEASSERT_LEVEL):
                self.assertEqual(await stream_get(ctx, dut.o_stream), expected)

        sim = Simulator(dut)
        sim.add_clock(1e-6)
        sim.add_testbench(testbench)
        with sim.write_vcd("test_uart_component_rts.vcd"):
            sim.run()

    def test_tx_inverted_pin_emits_expected_bytes(self):
        payload = b"10 PRINT 456\x1A"
        parsed_args = self._parse_args("--rx - --tx B3# --baud 1200")
        assembly = SimulationAssembly()
        applet = self.applet_cls(assembly)
        applet.build(parsed_args)

        tx_pin = assembly.get_pin("B3")
        bit_cyc = round(1 / (parsed_args.baud * assembly.sys_clk_period))
        received = bytearray()

        async def tx_monitor(ctx):
            def tx_level():
                # Decode the raw physical pin waveform back to a conventional idle-high UART.
                return ctx.get(tx_pin.o) ^ 1

            while len(received) < len(payload):
                while tx_level() == 1:
                    await ctx.tick()

                await ctx.tick().repeat(bit_cyc + bit_cyc // 2)

                value = 0
                for bitno in range(8):
                    value |= tx_level() << bitno
                    if bitno != 7:
                        await ctx.tick().repeat(bit_cyc)

                await ctx.tick().repeat(bit_cyc)
                self.assertEqual(tx_level(), 1)
                received.append(value)
                await ctx.tick()

        assembly.add_testbench(tx_monitor, background=True)

        async def main(ctx):
            await applet.setup(parsed_args)
            await applet.uart_iface.write(payload, flush=True)
            self.assertEqual(received, payload)

        assembly.run(main, vcd_file="test_uart_tx_inverted_pin.vcd")

    def test_flush_waits_for_cts_before_transmitting(self):
        payload = b"A"
        parsed_args = self._parse_args("--rx - --tx B3 --cts B0 --baud 1200")
        assembly = SimulationAssembly()
        applet = self.applet_cls(assembly)
        applet.build(parsed_args)

        cts_pin = assembly.get_pin("B0")
        tx_pin = assembly.get_pin("B3")
        bit_cyc = round(1 / (parsed_args.baud * assembly.sys_clk_period))
        received = bytearray()
        state = {"write_done": False}

        async def tx_monitor(ctx):
            while len(received) < len(payload):
                while ctx.get(tx_pin.o) == 1:
                    await ctx.tick()

                await ctx.tick().repeat(bit_cyc + bit_cyc // 2)

                value = 0
                for bitno in range(8):
                    value |= ctx.get(tx_pin.o) << bitno
                    if bitno != 7:
                        await ctx.tick().repeat(bit_cyc)

                await ctx.tick().repeat(bit_cyc)
                self.assertEqual(ctx.get(tx_pin.o), 1)
                received.append(value)
                await ctx.tick()

        async def cts_driver(ctx):
            ctx.set(cts_pin.i, 1)
            await ctx.tick().repeat(bit_cyc * 4)
            self.assertFalse(state["write_done"])
            self.assertEqual(ctx.get(tx_pin.o), 1)
            ctx.set(cts_pin.i, 0)

        assembly.add_testbench(tx_monitor, background=True)
        assembly.add_testbench(cts_driver, background=True)

        async def main(ctx):
            await applet.setup(parsed_args)
            await applet.uart_iface.write(payload, flush=True)
            state["write_done"] = True
            self.assertEqual(received, payload)

        assembly.run(main, vcd_file="test_uart_cts_flush_wait.vcd")

    def test_flush_waits_for_second_byte_when_cts_blocks_between_frames(self):
        payload = b"AB"
        parsed_args = self._parse_args("--rx - --tx B3 --cts B0 --baud 1200")
        assembly = SimulationAssembly()
        applet = self.applet_cls(assembly)
        applet.build(parsed_args)

        cts_pin = assembly.get_pin("B0")
        tx_pin = assembly.get_pin("B3")
        bit_cyc = round(1 / (parsed_args.baud * assembly.sys_clk_period))
        received = bytearray()
        state = {"write_done": False}

        async def tx_monitor(ctx):
            while len(received) < len(payload):
                while ctx.get(tx_pin.o) == 1:
                    await ctx.tick()

                await ctx.tick().repeat(bit_cyc + bit_cyc // 2)

                value = 0
                for bitno in range(8):
                    value |= ctx.get(tx_pin.o) << bitno
                    if bitno != 7:
                        await ctx.tick().repeat(bit_cyc)

                await ctx.tick().repeat(bit_cyc)
                self.assertEqual(ctx.get(tx_pin.o), 1)
                received.append(value)
                await ctx.tick()

        async def cts_driver(ctx):
            ctx.set(cts_pin.i, 0)
            while ctx.get(tx_pin.o) == 1:
                await ctx.tick()

            await ctx.tick().repeat(bit_cyc * 9 + bit_cyc // 2)
            ctx.set(cts_pin.i, 1)
            await ctx.tick().repeat(bit_cyc * 3)

            self.assertEqual(received, bytearray(b"A"))
            self.assertFalse(state["write_done"])
            self.assertEqual(ctx.get(tx_pin.o), 1)

            ctx.set(cts_pin.i, 0)

        assembly.add_testbench(tx_monitor, background=True)
        assembly.add_testbench(cts_driver, background=True)

        async def main(ctx):
            await applet.setup(parsed_args)
            await applet.uart_iface.write(payload, flush=True)
            state["write_done"] = True
            self.assertEqual(received, payload)

        assembly.run(main, vcd_file="test_uart_cts_two_byte_flush_wait.vcd")

    def test_flush_starts_transmitting_without_cts(self):
        payload = b"A"
        parsed_args = self._parse_args("--rx - --tx B3 --baud 1200")
        assembly = SimulationAssembly()
        applet = self.applet_cls(assembly)
        applet.build(parsed_args)

        tx_pin = assembly.get_pin("B3")
        state = {"write_started": False, "start_seen": False}

        async def tx_watchdog(ctx):
            while not state["write_started"]:
                await ctx.tick()

            for _ in range(8):
                if ctx.get(tx_pin.o) == 0:
                    state["start_seen"] = True
                    return
                await ctx.tick()

            self.fail("TX did not leave idle promptly without CTS")

        assembly.add_testbench(tx_watchdog, background=True)

        async def main(ctx):
            await applet.setup(parsed_args)
            state["write_started"] = True
            await applet.uart_iface.write(payload, flush=True)
            self.assertTrue(state["start_seen"])

        assembly.run(main, vcd_file="test_uart_no_cts_start.vcd")
