from amaranth import *
from amaranth.lib import io
from amaranth.sim import Simulator

from glasgow.gateware.ports import PortGroup
from glasgow.gateware.stream import stream_get
from glasgow.simulation.assembly import SimulationAssembly
from ... import *
from . import UARTApplet, UARTComponent


class UARTAppletTestCase(GlasgowAppletV2TestCase, applet=UARTApplet):
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
