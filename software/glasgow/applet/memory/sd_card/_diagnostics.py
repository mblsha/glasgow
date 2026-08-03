from enum import IntFlag

from amaranth.lib import data


__all__ = [
    "FPGA_DIAGNOSTICS_LAYOUT", "ProtocolErrorSource", "decode_fpga_diagnostics",
]


class ProtocolErrorSource(IntFlag):
    COMMAND_FRAME = 1 << 0
    COMMAND_CRC = 1 << 1
    DATA = 1 << 2
    HOST = 1 << 3


# A single control-register transaction latches this complete layout. Keeping the schema in one
# place makes the gateware packing and host decoding change together.
FPGA_DIAGNOSTICS_LAYOUT = data.StructLayout({
    "sd_clocks": 32,
    "command_low_clocks": 32,
    "commands": 32,
    "reads": 32,
    "writes": 32,
    "protocol_errors": 16,
    "protocol_error_sources": 4,
    "backend_errors": 16,
    "stale_replies": 16,
    "buffer_faults": 16,
    "prefetch_waits": 32,
    "prefetch_wait_cycles": 32,
    "max_prefetch_wait_cycles": 32,
    "max_host_service_latency_cycles": 32,
    "card_state": 4,
    "data_mode": 4,
    "read_bank_states": 6,
    "host_transaction_phase": 2,
    "host_reply_header_bytes": 4,
    "host_reply_payload_bytes": 10,
})


def decode_fpga_diagnostics(value):
    """Decode an integer-valued packed diagnostics register into named fields."""
    if hasattr(value, "as_bits"):
        value = value.as_bits()
    value = int(value)
    return {
        name: (value >> FPGA_DIAGNOSTICS_LAYOUT[name].offset) &
              ((1 << FPGA_DIAGNOSTICS_LAYOUT[name].width) - 1)
        for name in FPGA_DIAGNOSTICS_LAYOUT.members
    }
