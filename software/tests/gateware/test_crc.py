from collections.abc import Iterable
import random
import unittest
from amaranth import *
from amaranth.lib import crc
from amaranth.sim import Simulator

from glasgow.gateware.stream import stream_put, stream_assert
from glasgow.gateware.crc import *


END = object()


def run(dut, input: list[Iterable[int]], output: list[Iterable[int]], *, error=False, end=False):
    async def testbench_i(ctx):
        for packet in input:
            for index, byte in enumerate(packet):
                await stream_put(ctx, dut.i, {
                    "data":  byte,
                    "first": index == 0,
                    "last":  not end and index == len(packet) - 1,
                })
            if end:
                await stream_put(ctx, dut.i, {
                    "end":   1,
                })

    async def testbench_o(ctx):
        for packet in output:
            for index, byte in enumerate(packet):
                await stream_assert(ctx, dut.o, {
                    "data":  byte,
                    "first": index == 0,
                    "last":  not error and index == len(packet) - 1
                })

    sim = Simulator(dut)
    sim.add_clock(1e-6)
    sim.add_testbench(testbench_i)
    sim.add_testbench(testbench_o)
    with sim.write_vcd("crc.vcd"):
        sim.run()


def append(algorithm, data, *, data_width=8, flip=False):
    checksum = algorithm(data_width).compute(data)
    if flip:
        checksum ^= 1
    return data + checksum.to_bytes(algorithm.crc_width // data_width, "little")


def run_frame(dut, input, output, *, error=False):
    async def testbench_i(ctx):
        for byte in input:
            await stream_put(ctx, dut.i, {"data": byte})
        await stream_put(ctx, dut.i, {"end": 1})

    async def testbench_o(ctx):
        for byte in output:
            await stream_assert(ctx, dut.o, {"data": byte})
        expected_end = {"end": 1}
        if "error" in dut.o.p.shape().members:
            expected_end["error"] = error
        await stream_assert(ctx, dut.o, expected_end)

    sim = Simulator(dut)
    sim.add_clock(1e-6)
    sim.add_testbench(testbench_i)
    sim.add_testbench(testbench_o)
    sim.run()


def run_frame_backpressured(dut, input_tokens, output_tokens, *, seed):
    input_random = random.Random(seed)
    output_random = random.Random(seed ^ 0x5a5a)

    async def testbench_i(ctx):
        for token in input_tokens:
            delay = input_random.randrange(4)
            if delay:
                await ctx.tick().repeat(delay)
            ctx.set(dut.i.payload, token)
            ctx.set(dut.i.valid, 1)
            await ctx.tick().until(dut.i.ready)
            ctx.set(dut.i.valid, 0)

    async def testbench_o(ctx):
        for expected in output_tokens:
            for attempt in range(10000):
                ready = output_random.randrange(4) != 0 or attempt % 8 == 7
                ctx.set(dut.o.ready, ready)
                _, _, valid, payload = await ctx.tick().sample(dut.o.valid, dut.o.payload)
                if valid and ready:
                    for name, value in expected.items():
                        assert payload[name] == value, \
                            f"payload.{name}: {payload[name]!r} != {value!r}"
                    break
            else:
                raise AssertionError("output did not become valid within 10000 cycles")
        ctx.set(dut.o.ready, 0)

    sim = Simulator(dut)
    sim.add_clock(1e-6)
    sim.add_testbench(testbench_i)
    sim.add_testbench(testbench_o)
    sim.run()


class PacketChecksumTestCase(unittest.TestCase):
    def test_append(self):
        algorithm = crc.catalog.CRC32_ETHERNET
        for data in (b"\0", b"a", b"ab", b"abcd", b"abcde"):
            run(ChecksumAppender(algorithm), [data], [append(algorithm, data)])

    def test_verify_ok(self):
        algorithm = crc.catalog.CRC32_ETHERNET
        for end in (False, True):
            for data in (b"\0", b"a", b"ab", b"abcd", b"abcde"):
                run(ChecksumVerifier(algorithm), [append(algorithm, data)], [data],
                    end=end)

    def test_verify_fail(self):
        algorithm = crc.catalog.CRC32_ETHERNET
        for end in (False, True):
            for data in (b"\0", b"a", b"ab", b"abcd", b"abcde"):
                run(ChecksumVerifier(algorithm), [append(algorithm, data, flip=True)], [data],
                    error=True, end=end)


class FrameChecksumTestCase(unittest.TestCase):
    algorithm = crc.Algorithm(
        crc_width=16, polynomial=0x1021, initial_crc=0,
        reflect_input=False, reflect_output=False, xor_output=0)

    def test_append(self):
        for data in (b"", b"a", b"123456789"):
            run_frame(FrameChecksumAppender(self.algorithm), data,
                append(self.algorithm, data))

    def test_verify_ok(self):
        for data in (b"", b"a", b"123456789"):
            run_frame(FrameChecksumVerifier(self.algorithm), append(self.algorithm, data), data)

    def test_verify_fail(self):
        for data in (b"", b"a", b"123456789"):
            run_frame(FrameChecksumVerifier(self.algorithm), append(self.algorithm, data, flip=True),
                data, error=True)

    def test_verify_too_short(self):
        run_frame(FrameChecksumVerifier(self.algorithm), b"\0", b"", error=True)

    def test_error_boundary_resynchronizes_next_frame(self):
        dut = FrameChecksumVerifier(self.algorithm)

        async def testbench_i(ctx):
            await stream_put(ctx, dut.i, {"data": 0x41})
            await stream_put(ctx, dut.i, {"end": 1, "error": 1})
            for byte in append(self.algorithm, b"B"):
                await stream_put(ctx, dut.i, {"data": byte})
            await stream_put(ctx, dut.i, {"end": 1})

        async def testbench_o(ctx):
            await stream_assert(ctx, dut.o, {"end": 1, "error": 1})
            await stream_assert(ctx, dut.o, {"data": ord("B")})
            await stream_assert(ctx, dut.o, {"end": 1, "error": 0})

        sim = Simulator(dut)
        sim.add_clock(1e-6)
        sim.add_testbench(testbench_i)
        sim.add_testbench(testbench_o)
        sim.run()

    def test_randomized_backpressure(self):
        payload = bytes(range(256)) * 2
        encoded = append(self.algorithm, payload)

        run_frame_backpressured(
            FrameChecksumAppender(self.algorithm),
            [{"data": byte} for byte in payload] + [{"end": 1}],
            [{"data": byte} for byte in encoded] + [{"end": 1}],
            seed=0x63726361)
        run_frame_backpressured(
            FrameChecksumVerifier(self.algorithm),
            [{"data": byte} for byte in encoded] + [{"end": 1}],
            [{"data": byte} for byte in payload] + [{"end": 1, "error": 0}],
            seed=0x63726376)
