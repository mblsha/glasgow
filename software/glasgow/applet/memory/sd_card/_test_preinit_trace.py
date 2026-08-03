import json
from pathlib import Path
import tempfile
import unittest

from . import ProtocolErrorSource, SDCardDiagnostics
from ._preinit_trace import PreinitTrace, command_framing_bounds


class PreinitTraceTestCase(unittest.TestCase):
    def test_records_flushed_run_scoped_diagnostics(self):
        diagnostics = SDCardDiagnostics(
            sd_clocks=1234,
            command_low_clocks=321,
            commands=17,
            last_command=7,
            reads=8,
            writes=0,
            protocol_errors=65,
            protocol_error_sources=ProtocolErrorSource.COMMAND_FRAME,
            early_marker=0x3,
            fpga_backend_errors=0,
            stale_replies=0,
            buffer_faults=0,
            prefetch_waits=0,
            prefetch_wait_cycles=0,
            max_prefetch_wait_cycles=0,
            max_host_service_latency_cycles=0,
            host_backend_errors=0,
            durable_flushes=0,
            backend_calls=8,
            max_backend_call_seconds=0.001,
            card_state=4,
            data_mode=0,
            read_bank_states=0,
            host_transaction_phase=0,
            host_reply_header_bytes=0,
            host_reply_payload_bytes=0,
        )
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "preinit.jsonl"
            trace = PreinitTrace(path)
            trace.record_diagnostics(
                "rail-fell", diagnostics,
                generation=0x1234, active=False, voltage=0.2)

            # Each record is flushed immediately, before normal applet shutdown.
            live_records = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual([record["event"] for record in live_records], [
                "trace-start", "rail-fell",
            ])
            self.assertEqual(live_records[1]["generation"], 0x1234)
            self.assertEqual(live_records[1]["diagnostics"]["commands"], 17)
            self.assertEqual(live_records[1]["diagnostics"]["last_command"], 7)
            framing = live_records[1]["diagnostics"]["command_framing"]
            self.assertTrue(framing["available"])
            self.assertEqual(framing["frame_clocks"], 816)
            self.assertEqual(framing["non_frame_clocks"], 418)
            self.assertEqual(framing["frame_high_clocks_min"], 495)
            self.assertEqual(framing["frame_high_clocks_max"], 816)
            self.assertEqual(live_records[1]["diagnostics"]["protocol_errors"], 65)
            self.assertEqual(
                live_records[1]["diagnostics"]["protocol_error_sources"], 1)
            self.assertEqual(live_records[1]["diagnostics"]["early_marker"], 0x3)
            self.assertEqual(live_records[1]["voltage"], 0.2)
            self.assertEqual(live_records[0]["run_id"], live_records[1]["run_id"])

            trace.close()
            trace.close()
            records = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(records[-1]["event"], "trace-stop")

            second = PreinitTrace(path)
            second.close()
            appended = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertNotEqual(appended[0]["run_id"], appended[-1]["run_id"])

    def test_bounds_native_gpio_failure_without_gateware_changes(self):
        framing = command_framing_bounds(1041, 916, 21)
        self.assertEqual(framing, {
            "available": True,
            "complete_frames": 21,
            "frame_clocks": 1008,
            "non_frame_clocks": 33,
            "wire_high_clocks": 125,
            "wire_low_clocks": 916,
            "frame_high_clocks_min": 92,
            "frame_high_clocks_max": 125,
            "frame_low_clocks_min": 883,
            "frame_low_clocks_max": 916,
        })

    def test_omits_bounds_when_counter_mode_is_not_cumulative(self):
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "preinit.jsonl"
            trace = PreinitTrace(path, derive_command_framing=False)
            diagnostics = SDCardDiagnostics(
                sd_clocks=0, command_low_clocks=0, commands=0, last_command=0,
                reads=0, writes=0, protocol_errors=0,
                protocol_error_sources=ProtocolErrorSource(0), early_marker=0,
                fpga_backend_errors=0, stale_replies=0, buffer_faults=0,
                prefetch_waits=0, prefetch_wait_cycles=0,
                max_prefetch_wait_cycles=0, max_host_service_latency_cycles=0,
                host_backend_errors=0, durable_flushes=0, backend_calls=0,
                max_backend_call_seconds=0, card_state=0, data_mode=0,
                read_bank_states=0, host_transaction_phase=0,
                host_reply_header_bytes=0, host_reply_payload_bytes=0)
            trace.record_diagnostics(
                "sd-progress", diagnostics, generation=1, active=True)
            trace.close()
            records = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertNotIn("command_framing", records[1]["diagnostics"])


if __name__ == "__main__":
    unittest.main()
