"""Machine-readable SD activity and target-power trace for pre-init diagnostics."""

from dataclasses import asdict
import json
from pathlib import Path
import secrets
import time


COMMAND_FRAME_CLOCKS = 48


def command_framing_bounds(sd_clocks, command_low_clocks, commands):
    """Bound CMD-high/low samples that occurred inside complete 48-clock frames."""
    sd_clocks = int(sd_clocks)
    command_low_clocks = int(command_low_clocks)
    commands = int(commands)
    frame_clocks = commands * COMMAND_FRAME_CLOCKS
    if (sd_clocks < 0 or command_low_clocks < 0 or commands < 0 or
            command_low_clocks > sd_clocks or frame_clocks > sd_clocks):
        return {
            "available": False,
            "reason": "command counters are not cumulative totals",
        }

    wire_high_clocks = sd_clocks - command_low_clocks
    non_frame_clocks = sd_clocks - frame_clocks
    return {
        "available": True,
        "complete_frames": commands,
        "frame_clocks": frame_clocks,
        "non_frame_clocks": non_frame_clocks,
        "wire_high_clocks": wire_high_clocks,
        "wire_low_clocks": command_low_clocks,
        "frame_high_clocks_min": max(0, wire_high_clocks - non_frame_clocks),
        "frame_high_clocks_max": min(wire_high_clocks, frame_clocks),
        "frame_low_clocks_min": max(0, command_low_clocks - non_frame_clocks),
        "frame_low_clocks_max": min(command_low_clocks, frame_clocks),
    }


class PreinitTrace:
    """Append one flushed JSON object per observable pre-init event."""

    SCHEMA = 1

    def __init__(self, path, *, derive_command_framing=True):
        self.path = Path(path)
        self.run_id = secrets.token_hex(8)
        self._derive_command_framing = derive_command_framing
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
        diagnostic_fields = asdict(diagnostics)
        if self._derive_command_framing:
            diagnostic_fields["command_framing"] = command_framing_bounds(
                diagnostics.sd_clocks, diagnostics.command_low_clocks,
                diagnostics.commands)
        self.record(
            event,
            generation=generation,
            active=active,
            diagnostics=diagnostic_fields,
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
