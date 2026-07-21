from amaranth.lib import wiring
from amaranth.lib.wiring import In, Out

from ._gateware_support import CardState, DataMode


__all__ = ["CommandDataSignature", "DataHostSignature"]


class CommandDataSignature(wiring.Signature):
    """Control/status channel, oriented from the command engine toward the data engine."""

    def __init__(self):
        super().__init__({
            # Command-owned transfer state consumed by the data engine.
            "card_state": Out(CardState, init=CardState.Idle),
            "data_mode": Out(DataMode, init=DataMode.Idle),
            "bus_width_4": Out(1),
            "current_address": Out(32),
            "erase_start": Out(32),
            "erase_end": Out(32),
            "response_active": Out(1),
            "stop_requested": Out(1),

            # Data-engine state transitions captured by the command engine.
            "state_update_valid": In(1),
            "state_update": In(CardState),
            "mode_update_valid": In(1),
            "mode_update": In(DataMode),
            "address_update_valid": In(1),
            "address_update": In(32),
        })


class DataHostSignature(wiring.Signature):
    """Block request/result channel, oriented from the data engine toward the host engine."""

    def __init__(self):
        super().__init__({
            "read_request": Out(1),
            "read_request_accepted": In(1),
            "write_request": Out(1),
            "write_request_accepted": In(1),
            "erase_request": Out(1),
            "erase_request_accepted": In(1),

            "read_buffer_ready": In(1),
            "read_buffer_address": In(32),
            "read_buffer_consume": Out(1),
            "read_reply_error": In(1),
            "host_ack_valid": In(1),
            "host_ack_status": In(8),
            "host_ack_consume": Out(1),
        })
