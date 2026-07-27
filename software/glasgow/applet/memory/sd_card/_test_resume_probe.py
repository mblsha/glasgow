import unittest

from amaranth.sim import Simulator

from ._resume_probe import (
    RESUME_PROBE_BIT_CYCLES, ResumeProbeCapture,
)


def crc8_atm(payload):
    crc = 0
    for octet in payload:
        crc ^= octet
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xff if crc & 0x80 else (crc << 1) & 0xff
    return crc


def marker_frame(sequence, frame_type):
    body = bytes((1, sequence, frame_type, frame_type ^ 0xff))
    return b"\x55\xd3\x4d" + body + bytes((crc8_atm(body),))


class ResumeProbeCaptureTestCase(unittest.TestCase):
    def test_inverted_uart_frame_and_raw_edges(self):
        dut = ResumeProbeCapture()

        async def testbench(ctx):
            ctx.set(dut.clk_i, 0)
            ctx.set(dut.generation, 0x4a31)
            ctx.set(dut.armed, 1)
            await ctx.tick().repeat(12)
            self.assertEqual(ctx.get(dut.gate_ack), 1)
            self.assertEqual(ctx.get(dut.idle_low), 1)
            self.assertEqual(ctx.get(dut.arm_generation), 0x4a31)

            async def wire_bit(physical):
                ctx.set(dut.clk_i, physical)
                await ctx.tick().repeat(RESUME_PROBE_BIT_CYCLES)

            for octet in marker_frame(0x27, 0x10):
                await wire_bit(1)  # logical start=0, physically inverted
                for bit in range(8):
                    await wire_bit(not ((octet >> bit) & 1))
                await wire_bit(0)  # logical stop=1, physically inverted

            await ctx.tick().repeat(8)
            self.assertEqual(ctx.get(dut.valid_frames), 1)
            self.assertEqual(ctx.get(dut.last_sequence), 0x27)
            self.assertEqual(ctx.get(dut.last_type), 0x10)
            self.assertEqual(ctx.get(dut.framing_errors), 0)
            self.assertEqual(ctx.get(dut.protocol_errors), 0)
            self.assertGreater(ctx.get(dut.edge_count), 0)
            self.assertEqual(ctx.get(dut.dropped_edges), 0)
            self.assertEqual(ctx.get(dut.raw_count), ctx.get(dut.edge_count))

            # Disarming retains immutable evidence until the next arm edge.
            raw_count = ctx.get(dut.raw_count)
            ctx.set(dut.armed, 0)
            await ctx.tick().repeat(8)
            self.assertEqual(ctx.get(dut.gate_ack), 0)
            self.assertEqual(ctx.get(dut.raw_count), raw_count)
            self.assertEqual(ctx.get(dut.valid_frames), 1)

            ctx.set(dut.raw_index, 0)
            await ctx.tick()
            first_record = ctx.get(dut.raw_record)
            self.assertEqual(first_record >> 31, 1)
            self.assertGreater(first_record & 0x7fff_ffff, 0)

        simulator = Simulator(dut)
        simulator.add_clock(1 / 48_000_000)
        simulator.add_testbench(testbench)
        simulator.run()

    def test_bad_complement_is_rejected(self):
        dut = ResumeProbeCapture()

        async def testbench(ctx):
            ctx.set(dut.clk_i, 0)
            ctx.set(dut.armed, 1)
            await ctx.tick().repeat(12)

            body = bytes((1, 3, 0x10, 0x10))
            frame = b"\x55\xd3\x4d" + body + bytes((crc8_atm(body),))
            for octet in frame:
                for physical in (
                        1, *[not ((octet >> bit) & 1) for bit in range(8)], 0):
                    ctx.set(dut.clk_i, physical)
                    await ctx.tick().repeat(RESUME_PROBE_BIT_CYCLES)

            await ctx.tick().repeat(8)
            self.assertEqual(ctx.get(dut.valid_frames), 0)
            self.assertEqual(ctx.get(dut.protocol_errors), 1)

        simulator = Simulator(dut)
        simulator.add_clock(1 / 48_000_000)
        simulator.add_testbench(testbench)
        simulator.run()
