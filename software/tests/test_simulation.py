import asyncio
import unittest

from glasgow.simulation.assembly import SimulationPipe


class SimulationPipeTestCase(unittest.TestCase):
    def test_partial_reads_preserve_flush_boundaries(self):
        async def scenario():
            pipe = SimulationPipe(
                None,
                i_buffer=bytearray(b"abcDEFghi"),
                i_flush_offsets=[3, 6, 9],
                o_buffer=bytearray())

            self.assertTrue(pipe.flush_readable)
            self.assertEqual(bytes(await pipe.recv(2)), b"ab")
            self.assertEqual(await pipe.recv_flush(), b"c")
            self.assertEqual(await pipe.recv_until(b"E"), b"DE")
            self.assertEqual(await pipe.recv_flush(), b"F")
            self.assertEqual(await pipe.recv_flush(), b"ghi")
            self.assertFalse(pipe.flush_readable)
            self.assertEqual(pipe.readable, 0)

        asyncio.run(scenario())

    def test_reset_supports_one_directional_pipes(self):
        async def scenario():
            in_pipe = SimulationPipe(
                None, i_buffer=bytearray(b"input"), i_flush_offsets=[5], o_buffer=None)
            await in_pipe.reset()
            self.assertEqual(in_pipe.readable, 0)
            self.assertFalse(in_pipe.flush_readable)

            output = bytearray(b"output")
            out_pipe = SimulationPipe(
                None, i_buffer=None, i_flush_offsets=None, o_buffer=output)
            await out_pipe.reset()
            self.assertEqual(output, b"")

        asyncio.run(scenario())
