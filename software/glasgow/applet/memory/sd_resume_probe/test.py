import unittest

from glasgow.applet import GlasgowAppletV2TestCase, synthesis_test

from . import (
    SDResumeProbeApplet, ResumeProbeStatus, crc8_atm,
    decode_raw_marker_frames,
)
from ..sd_card._test_resume_probe import ResumeProbeCaptureTestCase


class SDResumeProbeAppletTestCase(
        GlasgowAppletV2TestCase, applet=SDResumeProbeApplet):
    @synthesis_test
    def test_build(self):
        self.assertBuilds([
            "-V", "B=3.3",
            "--clk", "B2", "--cmd", "B5", "--dat", "B1,B0,B7,B6",
        ], revision="C3")

    def test_arguments(self):
        args = self._parse_args([
            "-V", "B=3.3", "--control", "/tmp/zaurus-probe.sock",
        ])
        self.assertEqual(str(args.control), "/tmp/zaurus-probe.sock")
        self.assertEqual(str(args.clk), "B2")

    def test_independent_raw_decoder(self):
        body = bytes((1, 0x27, 0x10, 0xef))
        frame = b"\x55\xd3\x4d" + body + bytes((crc8_atm(body),))
        bit_ticks = 48_000_000 / 115_200
        tick = 1000
        level = 0
        records = []

        def transition(new_level, at_tick):
            nonlocal tick, level
            if new_level == level:
                return
            records.append({
                "index": len(records),
                "delta": at_tick - tick,
                "tick": at_tick,
                "level": new_level,
            })
            tick = at_tick
            level = new_level

        cursor = 1000 + bit_ticks
        for octet in frame:
            bits = (1, *[not ((octet >> bit) & 1) for bit in range(8)], 0)
            for physical in bits:
                transition(int(physical), round(cursor))
                cursor += bit_ticks

        status = ResumeProbeStatus(
            armed=True, gate_ack=True, idle_low=True, input_only_mask=0x3f,
            generation=1, arm_timestamp=1000, raw_count=len(records),
            edge_count=len(records), dropped_edges=0, timestamp_overflows=0,
            decoded_bytes=8, valid_frames=1, last_sequence=0x27,
            last_type=0x10, last_frame_timestamp=round(cursor),
            framing_errors=0, protocol_errors=0)
        decoded = decode_raw_marker_frames(status, records)
        self.assertEqual(decoded["frames"], [{
            "raw_byte_offset": 0,
            "start_tick": records[0]["tick"],
            "sequence": 0x27,
            "type": 0x10,
            "crc": frame[-1],
        }])
        self.assertAlmostEqual(decoded["timing"]["mean_ticks"], bit_ticks, delta=0.2)


if __name__ == "__main__":
    unittest.main()
