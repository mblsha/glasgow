import unittest

from amaranth import ClockDomain, Module
from amaranth.sim import Simulator

from glasgow.gateware import cobs as gateware_cobs

from . import BLOCK_SIZE
from ._buffer import BlockBuffer
from ._command import CommandEngine
from ._early_marker import EarlyMarkerCapture
from ._host import HostBlockEngine, TransactionPhase
from ._interfaces import ReadBufferState
from ._read_buffer import ReadBufferManager


class EarlyMarkerCaptureTestCase(unittest.TestCase):
    def test_retains_only_structurally_valid_complemented_markers(self):
        bit_cycles = 8
        m = Module()
        m.submodules.dut = dut = EarlyMarkerCapture(bit_cycles=bit_cycles)

        def crc8_atm(payload):
            crc = 0
            for octet in payload:
                crc ^= octet
                for _ in range(8):
                    crc = ((crc << 1) ^ 0x07) & 0xff if crc & 0x80 else (crc << 1) & 0xff
            return crc

        def marker_frame(sequence, marker):
            body = bytes((1, sequence, marker, marker ^ 0xff))
            return b"\x55\xd3\x4d" + body + bytes((crc8_atm(body),))

        async def testbench(ctx):
            async def drive(level, cycles=bit_cycles):
                ctx.set(dut.clk_i, level)
                await ctx.tick().repeat(cycles)

            async def send_octet(octet):
                await drive(1)  # Inverted logical start bit.
                for bit in range(8):
                    await drive(not ((octet >> bit) & 1))
                await drive(0)  # Inverted logical stop bit and idle.

            async def send_frame(frame):
                for octet in frame:
                    await send_octet(octet)
                await drive(0, bit_cycles * 2)

            await drive(0, bit_cycles * 2)
            await send_frame(marker_frame(1, 0x31))
            self.assertEqual(ctx.get(dut.last_marker), 0x31)

            bad_complement = bytearray(marker_frame(2, 0x32))
            bad_complement[6] ^= 1
            await send_frame(bad_complement)
            self.assertEqual(ctx.get(dut.last_marker), 0x31)

            bad_magic = bytearray(marker_frame(3, 0x33))
            bad_magic[2] ^= 1
            await send_frame(bad_magic)
            self.assertEqual(ctx.get(dut.last_marker), 0x31)

            await send_frame(marker_frame(4, 0x34))
            self.assertEqual(ctx.get(dut.last_marker), 0x34)

        simulator = Simulator(m)
        simulator.add_clock(1e-6)
        simulator.add_testbench(testbench)
        simulator.run()


class CommandEngineErrorBreakdownTestCase(unittest.TestCase):
    def test_framing_and_crc_errors_remain_strict_and_separate(self):
        m = Module()
        m.domains.sd = ClockDomain()
        m.submodules.dut = dut = CommandEngine()

        async def testbench(ctx):
            ctx.set(dut.enabled, 1)
            ctx.set(dut.cmd_i, 1)
            await ctx.tick("sd")

            async def send_word(word):
                for bit in range(47, -1, -1):
                    ctx.set(dut.cmd_i, (word >> bit) & 1)
                    await ctx.tick("sd")
                ctx.set(dut.cmd_i, 1)
                await ctx.tick("sd")

            # Valid start/transmission/end framing with a deliberately bad CRC7.
            await send_word((1 << 46) | 1)
            self.assertEqual(ctx.get(dut.protocol_errors), 1)
            self.assertEqual(ctx.get(dut.protocol_error_sources), 0b10)

            # An all-zero frame is malformed; it is still exactly one strict error.
            await send_word(0)
            self.assertEqual(ctx.get(dut.protocol_errors), 2)
            self.assertEqual(ctx.get(dut.protocol_error_sources), 0b11)

        simulator = Simulator(m)
        simulator.add_clock(1e-6, domain="sd")
        simulator.add_testbench(testbench)
        simulator.run()


class BlockBufferTestCase(unittest.TestCase):
    def test_collisions_are_counted_once_per_incident(self):
        m = Module()
        m.domains.sd = ClockDomain()
        m.submodules.dut = dut = BlockBuffer()

        async def testbench(ctx):
            ctx.set(dut.host.write_en, 1)
            ctx.set(dut.data.write_en, 1)
            self.assertEqual(ctx.get(dut.collision), 1)
            await ctx.tick("sd")
            self.assertEqual(ctx.get(dut.collision_count), 1)
            self.assertEqual(ctx.get(dut.fault), 1)
            await ctx.tick("sd").repeat(3)
            self.assertEqual(ctx.get(dut.collision_count), 1)

            ctx.set(dut.host.write_en, 0)
            ctx.set(dut.data.write_en, 0)
            await ctx.tick("sd")
            ctx.set(dut.host.read_active, 1)
            ctx.set(dut.data.read_active, 1)
            await ctx.tick("sd")
            self.assertEqual(ctx.get(dut.collision_count), 2)
            self.assertEqual(ctx.get(dut.fault), 1)

            ctx.set(dut.host.read_active, 0)
            ctx.set(dut.data.read_active, 0)
            await ctx.tick("sd")

            ctx.set(dut.data.read_active, 1)
            ctx.set(dut.host.write_en, 1)
            ctx.set(dut.host.write_address, BLOCK_SIZE)
            ctx.set(dut.data.read_address, BLOCK_SIZE + 7)
            await ctx.tick("sd")
            self.assertEqual(ctx.get(dut.collision_count), 3)

        simulator = Simulator(m)
        simulator.add_clock(1e-6, domain="sd")
        simulator.add_testbench(testbench)
        simulator.run()


class HostBlockEngineTestCase(unittest.TestCase):
    def test_raw_read_fill_backpressures_during_sd_buffer_write(self):
        m = Module()
        m.domains.sd = ClockDomain()
        m.submodules.dut = dut = HostBlockEngine()

        async def testbench(ctx):
            ctx.set(dut.generation, 0x4321)
            ctx.set(dut.image_size, BLOCK_SIZE * 8)
            ctx.set(dut.storage.read.request, 1)
            ctx.set(dut.storage.read.request_address, BLOCK_SIZE)
            ctx.set(dut.storage.read.request_bank, 0)
            ctx.set(dut.storage.read.request_blocks, 1)
            ctx.set(dut.o_stream.ready, 1)

            for _ in range(8):
                await ctx.tick("sd")
                if ctx.get(dut.storage.read.request_accepted):
                    break
            else:
                self.fail("read request was not accepted")
            ctx.set(dut.storage.read.request, 0)

            for _ in range(32):
                await ctx.tick("sd")
                if ctx.get(dut.transaction_phase) == TransactionPhase.AwaitReply.value:
                    break
            else:
                self.fail("read transaction did not reach AwaitReply")

            # Let the reply FSM enter Raw-Read, then present one complete raw
            # word while the SD data engine owns the shared write port.
            await ctx.tick("sd")
            ctx.set(dut.read_i_stream.p.generation, 0x4321)
            ctx.set(dut.read_i_stream.p.data, 0xa5a5a5a5a5a5a5a5)
            ctx.set(dut.read_i_stream.p.last, 0)
            ctx.set(dut.read_i_stream.valid, 1)
            ctx.set(dut.buffer_write_blocked, 1)
            self.assertEqual(ctx.get(dut.read_i_stream.ready), 0)
            self.assertEqual(ctx.get(dut.buffer.write_en), 0)

            # Releasing the SD writer must consume and publish the exact same
            # queued word rather than dropping it during the collision window.
            ctx.set(dut.buffer_write_blocked, 0)
            self.assertEqual(ctx.get(dut.read_i_stream.ready), 1)
            self.assertEqual(ctx.get(dut.buffer.write_en), 0xff)

        simulator = Simulator(m)
        simulator.add_clock(1e-6, domain="sd")
        simulator.add_testbench(testbench)
        simulator.run()

    def test_ownership_violation_poisoning(self):
        m = Module()
        m.domains.sd = ClockDomain()
        m.submodules.dut = dut = BlockBuffer()

        async def testbench(ctx):
            ctx.set(dut.bank_states, ReadBufferState.Draining.value)
            ctx.set(dut.host.write_en, 1)
            ctx.set(dut.host.write_address, 0)
            self.assertEqual(ctx.get(dut.ownership_violation), 0)
            await ctx.tick("sd")
            self.assertEqual(ctx.get(dut.fault), 0)

            ctx.set(dut.bank_states, ReadBufferState.Empty.value)
            self.assertEqual(ctx.get(dut.ownership_violation), 1)
            await ctx.tick("sd")
            self.assertEqual(ctx.get(dut.fault), 1)
            self.assertEqual(ctx.get(dut.fault_count), 1)

        simulator = Simulator(m)
        simulator.add_clock(1e-6, domain="sd")
        simulator.add_testbench(testbench)
        simulator.run()


class ReadBufferManagerTestCase(unittest.TestCase):
    def test_full_bank_ownership_lifecycle(self):
        m = Module()
        m.domains.sd = ClockDomain()
        m.submodules.dut = dut = ReadBufferManager()

        async def testbench(ctx):
            address = BLOCK_SIZE * 3
            ctx.set(dut.client.lookup_address, address)
            ctx.set(dut.client.reserve_address, address)
            self.assertEqual(ctx.get(dut.client.reserve_ready), 1)
            ctx.set(dut.client.reserve, 1)
            await ctx.tick("sd")
            ctx.set(dut.client.reserve, 0)
            self.assertEqual(ctx.get(dut.client.filling), 1)
            self.assertEqual(ctx.get(dut.host.request), 1)
            bank = ctx.get(dut.host.request_bank)
            ctx.set(dut.host.request_accepted, 1)
            await ctx.tick("sd")
            ctx.set(dut.host.request_accepted, 0)

            ctx.set(dut.host.reply_ready, 1)
            ctx.set(dut.host.reply_address, address)
            ctx.set(dut.host.reply_bank, bank)
            await ctx.tick("sd")
            ctx.set(dut.host.reply_ready, 0)
            self.assertEqual(ctx.get(dut.client.ready), 1)

            ctx.set(dut.client.activate, 1)
            await ctx.tick("sd")
            ctx.set(dut.client.activate, 0)
            self.assertEqual(ctx.get(dut.client.active), 1)
            states = ctx.get(dut.client.bank_states)
            self.assertEqual((states >> (bank * 3)) & 0x7, ReadBufferState.Active.value)

            ctx.set(dut.client.release, 1)
            await ctx.tick("sd")
            ctx.set(dut.client.release, 0)
            self.assertEqual(ctx.get(dut.client.active), 0)
            self.assertEqual(ctx.get(dut.client.reserve_ready), 1)

        simulator = Simulator(m)
        simulator.add_clock(1e-6, domain="sd")
        simulator.add_testbench(testbench)
        simulator.run()

    def test_accepted_prefetch_is_drained_after_cancellation(self):
        m = Module()
        m.domains.sd = ClockDomain()
        m.submodules.dut = dut = ReadBufferManager()

        async def testbench(ctx):
            address = BLOCK_SIZE * 5
            ctx.set(dut.client.lookup_address, address)
            ctx.set(dut.client.reserve_address, address)
            ctx.set(dut.client.reserve, 1)
            await ctx.tick("sd")
            ctx.set(dut.client.reserve, 0)
            bank = ctx.get(dut.host.request_bank)
            ctx.set(dut.host.request_accepted, 1)
            await ctx.tick("sd")
            ctx.set(dut.host.request_accepted, 0)

            ctx.set(dut.client.reset, 1)
            await ctx.tick("sd")
            ctx.set(dut.client.reset, 0)
            states = ctx.get(dut.client.bank_states)
            self.assertEqual((states >> (bank * 3)) & 0x7, ReadBufferState.Draining.value)

            ctx.set(dut.host.reply_ready, 1)
            ctx.set(dut.host.reply_address, address)
            ctx.set(dut.host.reply_bank, bank)
            await ctx.tick("sd")
            ctx.set(dut.host.reply_ready, 0)
            ctx.set(dut.client.lookup_address, address)
            self.assertEqual(ctx.get(dut.client.ready), 1)
            self.assertEqual(ctx.get(dut.client.reply_fault), 0)
            states = ctx.get(dut.client.bank_states)
            self.assertEqual((states >> (bank * 3)) & 0x7, ReadBufferState.Ready.value)

        simulator = Simulator(m)
        simulator.add_clock(1e-6, domain="sd")
        simulator.add_testbench(testbench)
        simulator.run()

    def test_reply_completing_during_reset_becomes_empty(self):
        m = Module()
        m.domains.sd = ClockDomain()
        m.submodules.dut = dut = ReadBufferManager()

        async def testbench(ctx):
            address = BLOCK_SIZE * 7
            ctx.set(dut.client.reserve_address, address)
            ctx.set(dut.client.reserve, 1)
            await ctx.tick("sd")
            ctx.set(dut.client.reserve, 0)
            bank = ctx.get(dut.host.request_bank)
            ctx.set(dut.host.request_accepted, 1)
            await ctx.tick("sd")
            ctx.set(dut.host.request_accepted, 0)

            ctx.set(dut.client.reset, 1)
            ctx.set(dut.host.reply_ready, 1)
            ctx.set(dut.host.reply_address, address)
            ctx.set(dut.host.reply_bank, bank)
            await ctx.tick("sd")
            states = ctx.get(dut.client.bank_states)
            self.assertEqual((states >> (bank * 3)) & 0x7, ReadBufferState.Ready.value)

        simulator = Simulator(m)
        simulator.add_clock(1e-6, domain="sd")
        simulator.add_testbench(testbench)
        simulator.run()


class COBSRecoveryTestCase(unittest.TestCase):
    def test_decoder_resynchronizes_at_delimiter(self):
        decoder = gateware_cobs.Decoder(recover=True)
        expected = b"A\0B"

        async def sender(ctx):
            # A delimiter where a group header is required is malformed, then a complete valid
            # packet must still be accepted without resetting the decoder.
            for octet in b"\0" + gateware_cobs.encode(expected) + b"\0":
                ctx.set(decoder.i.payload, octet)
                ctx.set(decoder.i.valid, 1)
                await ctx.tick().until(decoder.i.ready)
                ctx.set(decoder.i.valid, 0)

        async def receiver(ctx):
            result = bytearray()
            ctx.set(decoder.o.ready, 1)
            while True:
                payload, = await ctx.tick().sample(decoder.o.payload).until(decoder.o.valid)
                if payload.end:
                    break
                result.append(payload.data)
            self.assertEqual(result, expected)

        simulator = Simulator(decoder)
        simulator.add_clock(1e-6)
        simulator.add_testbench(sender)
        simulator.add_testbench(receiver)
        simulator.run()
