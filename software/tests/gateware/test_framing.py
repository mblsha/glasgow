import unittest

from amaranth.lib import crc
from amaranth.sim import Simulator
from cobs import cobs

from glasgow.gateware.framing import COBSCRCFrameDecoder, COBSCRCFrameEncoder
from glasgow.gateware.stream import stream_assert, stream_get, stream_put


FRAME_CRC16 = crc.Algorithm(
    crc_width=16, polynomial=0x1021, initial_crc=0,
    reflect_input=False, reflect_output=False, xor_output=0)


def encoded_frame(payload):
    checksum = FRAME_CRC16(8).compute(payload).to_bytes(2, "little")
    return cobs.encode(payload + checksum) + b"\0"


class COBSCRCFrameTestCase(unittest.TestCase):
    def test_encoder(self):
        dut = COBSCRCFrameEncoder(FRAME_CRC16)
        payload = b"A\0frame"

        async def testbench_i(ctx):
            for byte in payload:
                await stream_put(ctx, dut.i, {"data": byte})
            await stream_put(ctx, dut.i, {"end": 1})

        async def testbench_o(ctx):
            for byte in encoded_frame(payload):
                self.assertEqual(await stream_get(ctx, dut.o), byte)

        simulator = Simulator(dut)
        simulator.add_clock(1e-6)
        simulator.add_testbench(testbench_i)
        simulator.add_testbench(testbench_o)
        simulator.run()

    def test_decoder_recovers_at_handshaked_error_boundary(self):
        dut = COBSCRCFrameDecoder(FRAME_CRC16)
        payload = b"next"

        async def testbench_i(ctx):
            # The unexpected delimiter truncates a COBS group after one byte.
            for byte in b"\x03A\0" + encoded_frame(payload):
                await stream_put(ctx, dut.i, byte)

        async def testbench_o(ctx):
            await ctx.tick().repeat(5)
            await stream_assert(ctx, dut.o, {"end": 1, "error": 1})
            for byte in payload:
                await stream_assert(ctx, dut.o, {"data": byte})
            await stream_assert(ctx, dut.o, {"end": 1, "error": 0})

        simulator = Simulator(dut)
        simulator.add_clock(1e-6)
        simulator.add_testbench(testbench_i)
        simulator.add_testbench(testbench_o)
        simulator.run()
