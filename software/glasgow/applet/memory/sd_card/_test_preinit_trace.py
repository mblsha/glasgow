import json
from pathlib import Path
import tempfile
import unittest

from . import ProtocolErrorSource, SDCardDiagnostics
from ._preinit_trace import PreinitTrace


class PreinitTraceTestCase(unittest.TestCase):
    def test_records_flushed_run_scoped_diagnostics(self):
        diagnostics = SDCardDiagnostics(
            sd_clocks=1234,
            command_low_clocks=321,
            commands=17,
            reads=8,
            writes=0,
            protocol_errors=65,
            protocol_error_sources=ProtocolErrorSource.COMMAND_FRAME,
            early_marker=0x31,
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
            self.assertEqual(live_records[1]["diagnostics"]["protocol_errors"], 65)
            self.assertEqual(
                live_records[1]["diagnostics"]["protocol_error_sources"], 1)
            self.assertEqual(live_records[1]["diagnostics"]["early_marker"], 0x31)
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


if __name__ == "__main__":
    unittest.main()
