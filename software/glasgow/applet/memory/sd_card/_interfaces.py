from amaranth.lib import enum, wiring
from amaranth.lib.wiring import In, Out

from ._gateware_support import BUFFER_BANK_BITS, CardState, DataMode


__all__ = [
    "CommandDataSignature", "DataHostSignature", "MutationHostSignature",
    "ReadBufferState", "ReadHostSignature",
]


class ReadBufferState(enum.Enum, shape=3):
    """Ownership state for each bank of the read ping-pong buffer."""

    Empty = 0
    Filling = 1
    Ready = 2
    Active = 3
    Draining = 4
    Mutation = 5


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
            "card_reset": Out(1),

            # Data-engine state transitions captured by the command engine.
            "state_update_valid": In(1),
            "state_update": In(CardState),
            "mode_update_valid": In(1),
            "mode_update": In(DataMode),
            "address_update_valid": In(1),
            "address_update": In(32),
        })


class ReadHostSignature(wiring.Signature):
    """Read-fill request/reply channel, oriented toward the host engine."""

    def __init__(self):
        super().__init__({
            "request": Out(1),
            "request_address": Out(32),
            "request_bank": Out(BUFFER_BANK_BITS),
            "request_blocks": Out(2),
            "request_accepted": In(1),
            "reply_ready": In(1),
            "reply_address": In(32),
            "reply_bank": In(BUFFER_BANK_BITS),
            "reply_consume": Out(1),
            "reply_error": In(1),
        })


class MutationHostSignature(wiring.Signature):
    """Write/erase request and acknowledgement channel, oriented toward the host engine."""

    def __init__(self):
        super().__init__({
            "write_request": Out(1),
            "write_bank": Out(BUFFER_BANK_BITS),
            "write_request_accepted": In(1),
            "erase_request": Out(1),
            "erase_request_accepted": In(1),
            "ack_valid": In(1),
            "ack_status": In(8),
            "ack_consume": Out(1),
        })


class DataHostSignature(wiring.Signature):
    """Block request/result channels, oriented from the data engine toward the host engine."""

    def __init__(self):
        super().__init__({
            "read": Out(ReadHostSignature()),
            "mutation": Out(MutationHostSignature()),
        })
