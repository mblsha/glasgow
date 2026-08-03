"""Machine-readable SD activity and target-power trace for pre-init diagnostics."""

from dataclasses import asdict
import json
from pathlib import Path
import secrets
import time


class PreinitTrace:
    """Append one flushed JSON object per observable pre-init event."""

    SCHEMA = 1

    def __init__(self, path):
        self.path = Path(path)
        self.run_id = secrets.token_hex(8)
        self._file = self.path.open("a", encoding="utf-8", buffering=1)
        self.record("trace-start")

    def record(self, event, **fields):
        record = {
            "schema": self.SCHEMA,
            "run_id": self.run_id,
            "event": event,
            "wall_time_ns": time.time_ns(),
            "monotonic_ns": time.monotonic_ns(),
            **fields,
        }
        self._file.write(json.dumps(record, sort_keys=True, separators=(",", ":")))
        self._file.write("\n")
        self._file.flush()

    def record_diagnostics(self, event, diagnostics, *, generation, active, **fields):
        self.record(
            event,
            generation=generation,
            active=active,
            diagnostics=asdict(diagnostics),
            **fields,
        )

    def close(self):
        if self._file is None:
            return
        try:
            self.record("trace-stop")
        finally:
            self._file.close()
            self._file = None
