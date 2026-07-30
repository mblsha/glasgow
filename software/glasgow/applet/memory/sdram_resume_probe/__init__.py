import asyncio
from dataclasses import asdict, dataclass
import enum
import hashlib
import json
import logging
from pathlib import Path

from amaranth import *
from amaranth.lib import cdc, io, memory, wiring
from amaranth.lib.wiring import In, Out

from glasgow.abstract import AbstractAssembly, GlasgowPin
from glasgow.applet import GlasgowAppletError, GlasgowAppletV2


__all__ = [
    "SDRAM_CAPTURE_DEPTH", "SDRAMCaptureMode", "SDRAMCaptureStatus",
    "SDRAMSignalProfile", "SDRAM_SIGNAL_PROFILES",
    "SDRAMResumeCapture", "SDRAMResumeProbeApplet",
    "SDRAMResumeProbeComponent", "SDRAMResumeProbeInterface",
    "decode_capture_record", "is_self_refresh_sample",
    "resolve_signal_profile", "resolve_arm_signatures",
    "validate_signal_profile_attestation",
]


SDRAM_CAPTURE_DEPTH = 1024

# The raw sample layout is deliberately stable because retained records are
# evidence artifacts, not merely an internal implementation detail.
SAMPLE_CKE = 0
SAMPLE_CS_N = 1
SAMPLE_RAS_N = 2
SAMPLE_CAS_N = 3
SAMPLE_WE_N = 4
SAMPLE_BA0 = 5
SAMPLE_BA1 = 6
SAMPLE_ADDRESS_0 = 7
SAMPLE_ADDRESS_1 = 8
SAMPLE_ADDRESS_2 = 9
SAMPLE_ADDRESS_3 = 10
SAMPLE_ADDRESS_4 = 11
SAMPLE_WIDTH = 12

# Backward-compatible names for the original v9-a0018330 fixture. Raw capture
# records are positional; their semantic address names come from the attested
# signal profile embedded in every host report.
SAMPLE_A2 = SAMPLE_ADDRESS_0
SAMPLE_A3 = SAMPLE_ADDRESS_1
SAMPLE_A5 = SAMPLE_ADDRESS_2
SAMPLE_A6 = SAMPLE_ADDRESS_3
SAMPLE_A7 = SAMPLE_ADDRESS_4

COMMANDS = {
    (1, 1, 1): "nop",
    (1, 1, 0): "burst-terminate",
    (1, 0, 1): "read",
    (1, 0, 0): "write",
    (0, 1, 1): "activate",
    (0, 1, 0): "precharge",
    (0, 0, 1): "auto-refresh",
    (0, 0, 0): "mode-register-set",
}


@dataclass(frozen=True)
class SDRAMSignalProfile:
    name: str
    prepared_target_physical: int
    address_signals: tuple[int, int, int, int, int]
    active_row_indices: tuple[int, int]
    read_indices: tuple[int, int, int, int]
    zero_signature: int = 0x00
    target_signature: int = 0xfc

    def metadata(self):
        active_row_signals = tuple(
            self.address_signals[index]
            for index in self.active_row_indices)
        read_signals = tuple(
            self.address_signals[index]
            for index in self.read_indices)
        mapping = {
            "schema": 1,
            "name": self.name,
            "prepared_target_physical":
                f"0x{self.prepared_target_physical:08x}",
            "address_pin_order": [
                f"A{signal}" for signal in self.address_signals],
            "active_row_signature_order": [
                f"A{signal}" for signal in active_row_signals],
            "read_signature_order": [
                f"A{signal}" for signal in read_signals],
            "signature_layout": {
                "bank": "bits 1:0 = Cat(BA0, BA1)",
                "active_row": "bits 3:2 in listed order",
                "read": "bits 7:4 in listed order",
            },
            "zero_signature": self.zero_signature,
            "target_signature": self.target_signature,
        }
        canonical = json.dumps(
            mapping, sort_keys=True, separators=(",", ":")).encode()
        return {
            **mapping,
            "mapping_sha256": hashlib.sha256(canonical).hexdigest(),
        }


SDRAM_SIGNAL_PROFILES = {
    "v9-a0018330": SDRAMSignalProfile(
        name="v9-a0018330",
        prepared_target_physical=0xa0018330,
        address_signals=(2, 3, 5, 6, 7),
        active_row_indices=(2, 3),
        read_indices=(0, 1, 3, 4),
    ),
    "v14-a00302c4": SDRAMSignalProfile(
        name="v14-a00302c4",
        prepared_target_physical=0xa00302c4,
        address_signals=(0, 4, 5, 6, 7),
        active_row_indices=(3, 4),
        read_indices=(0, 1, 2, 4),
    ),
    "v15-a0030280": SDRAMSignalProfile(
        name="v15-a0030280",
        prepared_target_physical=0xa0030280,
        address_signals=(0, 4, 5, 6, 7),
        active_row_indices=(3, 4),
        read_indices=(0, 1, 2, 4),
        target_signature=0xcc,
    ),
}


def resolve_signal_profile(profile):
    if isinstance(profile, SDRAMSignalProfile):
        return profile
    try:
        return SDRAM_SIGNAL_PROFILES[profile]
    except (KeyError, TypeError) as error:
        raise SDRAMResumeProbeError(
            f"unknown SDRAM signal profile {profile!r}") from error


def resolve_arm_signatures(
        signal_profile, zero_signature=None, target_signature=None):
    profile = resolve_signal_profile(signal_profile)
    if zero_signature is None:
        zero_signature = profile.zero_signature
    if target_signature is None:
        target_signature = profile.target_signature
    if zero_signature != profile.zero_signature:
        raise SDRAMResumeProbeError(
            f"zero signature 0x{zero_signature:02x} does not match "
            f"profile {profile.name} value 0x{profile.zero_signature:02x}")
    if target_signature != profile.target_signature:
        raise SDRAMResumeProbeError(
            f"target signature 0x{target_signature:02x} does not match "
            f"profile {profile.name} value 0x{profile.target_signature:02x}")
    return zero_signature, target_signature


def validate_signal_profile_attestation(
        signal_profile, address_profile, mapping_sha256):
    profile = resolve_signal_profile(signal_profile)
    metadata = profile.metadata()
    if address_profile != profile.name:
        raise SDRAMResumeProbeError(
            f"arm address profile {address_profile!r} does not match "
            f"built profile {profile.name!r}")
    if mapping_sha256 != metadata["mapping_sha256"]:
        raise SDRAMResumeProbeError(
            "arm mapping SHA-256 does not match the built profile")


class SDRAMCaptureMode(enum.IntEnum):
    DEEP_SELF_REFRESH = 0
    CALIBRATION_FILTERED = 1
    RAW_IMMEDIATE = 2


class SDRAMResumeProbeError(GlasgowAppletError):
    pass


@dataclass(frozen=True)
class SDRAMCaptureStatus:
    armed: bool
    input_only_mask: int
    generation: int
    mode: int
    zero_signature: int
    target_signature: int
    minimum_zero_reads: int
    triggered: bool
    self_refresh_seen: bool
    trigger_sample: int
    trigger_clock_index: int
    last_sample: int
    last_sample_is_self_refresh: bool
    frozen: bool
    overflow: bool
    target_seen: bool
    event_count: int
    clock_edges: int
    zero_reads: int
    target_reads: int
    target_reads_before_boundary: int


def decode_capture_record(value, signal_profile="v9-a0018330"):
    profile = resolve_signal_profile(signal_profile)
    sample = value & 0x0fff
    signals = {
        "cke": (sample >> SAMPLE_CKE) & 1,
        "cs_n": (sample >> SAMPLE_CS_N) & 1,
        "ras_n": (sample >> SAMPLE_RAS_N) & 1,
        "cas_n": (sample >> SAMPLE_CAS_N) & 1,
        "we_n": (sample >> SAMPLE_WE_N) & 1,
        "ba0": (sample >> SAMPLE_BA0) & 1,
        "ba1": (sample >> SAMPLE_BA1) & 1,
    }
    for index, address_signal in enumerate(profile.address_signals):
        signals[f"a{address_signal}"] = (
            sample >> (SAMPLE_ADDRESS_0 + index)) & 1
    command = (
        "deselect" if signals["cs_n"] else
        COMMANDS[(
            signals["ras_n"], signals["cas_n"], signals["we_n"])]
    )
    return {
        "raw": value,
        "sample": sample,
        "signals": signals,
        "command": command,
        "active_row_discriminator": (value >> 12) & 0x3,
        "active_row_signals": [
            f"A{profile.address_signals[index]}"
            for index in profile.active_row_indices],
        "active_row_valid": bool((value >> 14) & 1),
        "clock_index": (value >> 15) & 0xffff,
        "reserved_bit_31": (value >> 31) & 1,
    }


def is_self_refresh_sample(value):
    sample = value & 0x0fff
    return (
        not ((sample >> SAMPLE_CKE) & 1) and
        not ((sample >> SAMPLE_CS_N) & 1) and
        not ((sample >> SAMPLE_RAS_N) & 1) and
        not ((sample >> SAMPLE_CAS_N) & 1) and
        bool((sample >> SAMPLE_WE_N) & 1)
    )


_MISSING = object()


def require_request_bool(request, name, *, default=_MISSING):
    if name not in request:
        if default is _MISSING:
            raise SDRAMResumeProbeError(
                f"request is missing boolean {name!r}")
        value = default
    else:
        value = request[name]
    if not isinstance(value, bool):
        raise SDRAMResumeProbeError(
            f"request field {name!r} must be a JSON boolean")
    return value


def require_request_int(request, name, *, default=_MISSING):
    if name not in request:
        if default is _MISSING:
            raise SDRAMResumeProbeError(
                f"request is missing integer {name!r}")
        value = default
    else:
        value = request[name]
    if isinstance(value, bool) or not isinstance(value, int):
        raise SDRAMResumeProbeError(
            f"request field {name!r} must be a JSON integer")
    return value


def require_request_str(request, name, *, default=_MISSING):
    if name not in request:
        if default is _MISSING:
            raise SDRAMResumeProbeError(
                f"request is missing string {name!r}")
        value = default
    else:
        value = request[name]
    if not isinstance(value, str):
        raise SDRAMResumeProbeError(
            f"request field {name!r} must be a JSON string")
    return value


class SDRAMResumeCapture(wiring.Component):
    """Retain a bounded SDRAM command trace in the target clock domain.

    ``clk_i`` is the observed PXA255 ``SDCLK1`` signal and ``sample`` is the
    output of pin-local input flip-flops clocked by it. No oversampling clock
    is used. The final self-refresh-entry sample therefore remains observable
    even when the target immediately stops ``SDCLK1``.
    """

    armed: In(1)
    freeze: In(1)
    generation: In(16)
    mode: In(2)
    zero_signature: In(8)
    target_signature: In(8)
    minimum_zero_reads: In(8)

    clk_i: In(1)
    sample: In(SAMPLE_WIDTH)
    record_index: In(range(SDRAM_CAPTURE_DEPTH))

    armed_ack: Out(1)
    arm_generation: Out(16)
    arm_mode: Out(2)
    arm_zero_signature: Out(8)
    arm_target_signature: Out(8)
    arm_minimum_zero_reads: Out(8)
    triggered: Out(1)
    self_refresh_seen: Out(1)
    trigger_sample: Out(SAMPLE_WIDTH)
    trigger_clock_index: Out(16)
    last_sample: Out(SAMPLE_WIDTH)
    frozen: Out(1)
    overflow: Out(1)
    target_seen: Out(1)
    event_count: Out(range(SDRAM_CAPTURE_DEPTH + 1))
    clock_edges: Out(32)
    zero_reads: Out(16)
    target_reads: Out(16)
    target_reads_before_boundary: Out(16)
    record: Out(32)

    def __init__(self, *, signal_profile="v9-a0018330"):
        self.signal_profile = resolve_signal_profile(signal_profile)
        super().__init__()

    def elaborate(self, platform):
        m = Module()

        if platform is not None:
            # The observed SL-C860 bus is approximately 99.5 MHz. This is a
            # source-synchronous constraint, not a claim that ordinary Glasgow
            # 48 MHz sampling can resolve the bus.
            platform.add_clock_constraint(self.clk_i, 100e6)

        m.domains.sdram = cd_sdram = ClockDomain(
            reset_less=True, local=True)
        m.d.comb += cd_sdram.clk.eq(self.clk_i)

        armed_sdram = Signal()
        freeze_sdram = Signal()
        generation_sdram = Signal(16)
        mode_sdram = Signal(2)
        zero_signature_sdram = Signal(8)
        target_signature_sdram = Signal(8)
        minimum_zero_reads_sdram = Signal(8)
        for name, source, target in (
                ("armed_to_sdram", self.armed, armed_sdram),
                ("freeze_to_sdram", self.freeze, freeze_sdram),
                ("generation_to_sdram", self.generation, generation_sdram),
                ("mode_to_sdram", self.mode, mode_sdram),
                ("zero_signature_to_sdram",
                 self.zero_signature, zero_signature_sdram),
                ("target_signature_to_sdram",
                 self.target_signature, target_signature_sdram),
                ("minimum_zero_reads_to_sdram",
                 self.minimum_zero_reads, minimum_zero_reads_sdram)):
            m.submodules[name] = cdc.FFSynchronizer(
                source, target, o_domain="sdram")

        capture_memory = memory.Memory(
            shape=32, depth=SDRAM_CAPTURE_DEPTH, init=[])
        capture_write = capture_memory.write_port(domain="sdram")
        capture_read = capture_memory.read_port(domain="sync")
        m.submodules.capture_memory = capture_memory
        m.d.comb += [
            capture_write.en.eq(0),
            capture_write.addr.eq(0),
            capture_write.data.eq(0),
            capture_read.addr.eq(self.record_index),
            self.record.eq(capture_read.data),
        ]

        armed_ack_sdram = Signal()
        arm_generation_sdram = Signal(16)
        arm_mode_sdram = Signal(2)
        arm_zero_signature_sdram = Signal(8)
        arm_target_signature_sdram = Signal(8)
        arm_minimum_zero_reads_sdram = Signal(8)
        triggered_sdram = Signal()
        self_refresh_seen_sdram = Signal()
        trigger_sample_sdram = Signal(SAMPLE_WIDTH)
        trigger_clock_index_sdram = Signal(16)
        frozen_sdram = Signal()
        overflow_sdram = Signal()
        target_seen_sdram = Signal()
        event_count_sdram = Signal(range(SDRAM_CAPTURE_DEPTH + 1))
        clock_edges_sdram = Signal(32)
        zero_reads_sdram = Signal(16)
        zero_boundary_match_q = Signal()
        zero_boundary_seen_sdram = Signal()
        target_reads_sdram = Signal(16)
        target_reads_before_boundary_sdram = Signal(16)
        cke_q = Signal()

        # The wrapper retains raw pins in the iCE40 input flip-flops. Command
        # classification and RAM writes occur only after the clock has
        # resumed; the host-domain last-sample register covers a clock that
        # never resumes.
        sample_q = Signal(SAMPLE_WIDTH)
        sample_clock_index_q = Signal(16)
        sample_valid_q = Signal()
        record_q = Signal(32)
        store_q = Signal()
        zero_match_q = Signal()
        target_match_q = Signal()

        active_row = Array(Signal(2, name=f"active_row_{bank}")
                           for bank in range(4))
        active_valid = Array(Signal(name=f"active_valid_{bank}")
                             for bank in range(4))

        cke = self.sample[SAMPLE_CKE]
        cs_n = self.sample[SAMPLE_CS_N]
        ras_n = self.sample[SAMPLE_RAS_N]
        cas_n = self.sample[SAMPLE_CAS_N]
        we_n = self.sample[SAMPLE_WE_N]
        is_nop = ~cs_n & ras_n & cas_n & we_n
        is_non_nop = ~cs_n & ~is_nop
        is_auto_refresh = ~cs_n & ~ras_n & ~cas_n & we_n
        is_self_refresh_entry = cke_q & ~cke & is_auto_refresh

        sample_q_cs_n = sample_q[SAMPLE_CS_N]
        sample_q_ras_n = sample_q[SAMPLE_RAS_N]
        sample_q_cas_n = sample_q[SAMPLE_CAS_N]
        sample_q_we_n = sample_q[SAMPLE_WE_N]
        sample_q_bank = Cat(
            sample_q[SAMPLE_BA0], sample_q[SAMPLE_BA1])
        sample_q_row = Cat(
            *(
                sample_q[SAMPLE_ADDRESS_0 + index]
                for index in self.signal_profile.active_row_indices
            ))
        sample_q_column = Cat(
            *(
                sample_q[SAMPLE_ADDRESS_0 + index]
                for index in self.signal_profile.read_indices
            ))
        sample_q_is_read = (
            ~sample_q_cs_n & sample_q_ras_n &
            ~sample_q_cas_n & sample_q_we_n)
        sample_q_is_activate = (
            ~sample_q_cs_n & ~sample_q_ras_n &
            sample_q_cas_n & sample_q_we_n)
        sample_q_is_precharge = (
            ~sample_q_cs_n & ~sample_q_ras_n &
            sample_q_cas_n & ~sample_q_we_n)
        sample_q_is_auto_refresh = (
            ~sample_q_cs_n & ~sample_q_ras_n &
            ~sample_q_cas_n & sample_q_we_n)
        sample_q_is_mode_register_set = (
            ~sample_q_cs_n & ~sample_q_ras_n &
            ~sample_q_cas_n & ~sample_q_we_n)
        sample_q_active_row = active_row[sample_q_bank]
        sample_q_active_valid = active_valid[sample_q_bank]
        sample_q_read_signature = Cat(
            sample_q_bank, sample_q_active_row, sample_q_column)
        sample_q_zero_match = (
            sample_q_is_read & sample_q_active_valid &
            (sample_q_read_signature == arm_zero_signature_sdram))
        sample_q_target_match = (
            sample_q_is_read & sample_q_active_valid &
            (sample_q_read_signature == arm_target_signature_sdram))
        sample_q_filtered_match = (
            sample_q_zero_match | sample_q_target_match)
        sample_q_record = Cat(
            sample_q,
            sample_q_active_row,
            sample_q_active_valid,
            sample_clock_index_q,
            C(0, 1),
        )

        def clear_capture_state():
            assignments = [
                triggered_sdram.eq(0),
                self_refresh_seen_sdram.eq(0),
                trigger_sample_sdram.eq(0),
                trigger_clock_index_sdram.eq(0),
                frozen_sdram.eq(0),
                overflow_sdram.eq(0),
                target_seen_sdram.eq(0),
                event_count_sdram.eq(0),
                clock_edges_sdram.eq(0),
                zero_reads_sdram.eq(0),
                zero_boundary_match_q.eq(0),
                zero_boundary_seen_sdram.eq(0),
                target_reads_sdram.eq(0),
                target_reads_before_boundary_sdram.eq(0),
                cke_q.eq(cke),
                sample_q.eq(0),
                sample_clock_index_q.eq(0),
                sample_valid_q.eq(0),
                record_q.eq(0),
                store_q.eq(0),
                zero_match_q.eq(0),
                target_match_q.eq(0),
            ]
            for bank_index in range(4):
                assignments.extend((
                    active_row[bank_index].eq(0),
                    active_valid[bank_index].eq(0),
                ))
            return assignments

        with m.If(~armed_sdram):
            m.d.sdram += [
                armed_ack_sdram.eq(0),
                *clear_capture_state(),
            ]
        with m.Elif(~armed_ack_sdram):
            # All configuration inputs have been stable for the two-stage
            # synchronizer latency before acknowledgement is asserted.
            m.d.sdram += [
                armed_ack_sdram.eq(1),
                arm_generation_sdram.eq(generation_sdram),
                arm_mode_sdram.eq(mode_sdram),
                arm_zero_signature_sdram.eq(zero_signature_sdram),
                arm_target_signature_sdram.eq(target_signature_sdram),
                arm_minimum_zero_reads_sdram.eq(
                    minimum_zero_reads_sdram),
                *clear_capture_state(),
            ]
            with m.If(mode_sdram != SDRAMCaptureMode.DEEP_SELF_REFRESH):
                m.d.sdram += triggered_sdram.eq(1)
        with m.Else():
            m.d.sdram += [
                cke_q.eq(cke),
                clock_edges_sdram.eq(clock_edges_sdram + 1),
            ]

            with m.If(freeze_sdram):
                m.d.sdram += [
                    frozen_sdram.eq(1),
                    sample_valid_q.eq(0),
                    store_q.eq(0),
                    zero_match_q.eq(0),
                    target_match_q.eq(0),
                ]
            with m.Elif(~frozen_sdram):
                # Pipeline stage 2: write a fully registered record and update
                # the classification counters. The RAM address path is only a
                # registered valid bit plus the monotonic write pointer.
                with m.If(store_q):
                    with m.If(~event_count_sdram[-1]):
                        m.d.comb += [
                            capture_write.en.eq(1),
                            capture_write.addr.eq(
                                event_count_sdram[:10]),
                            capture_write.data.eq(record_q),
                        ]
                        m.d.sdram += event_count_sdram.eq(
                            event_count_sdram + 1)
                    with m.Else():
                        m.d.sdram += [
                            overflow_sdram.eq(1),
                            frozen_sdram.eq(1),
                        ]

                with m.If(zero_match_q):
                    with m.If(zero_reads_sdram != 0xffff):
                        m.d.sdram += zero_reads_sdram.eq(
                            zero_reads_sdram + 1)
                # The accepted boundary is at most 255 reads. Pipeline the
                # 8-bit equality and then retain a sticky bit, keeping both
                # the counter comparison and the target-match/freeze path
                # short at 100 MHz.
                m.d.sdram += zero_boundary_match_q.eq(
                    zero_match_q &
                    (zero_reads_sdram[:8] ==
                     arm_minimum_zero_reads_sdram - 1))
                with m.If(zero_boundary_match_q):
                    m.d.sdram += zero_boundary_seen_sdram.eq(1)
                with m.If(target_match_q):
                    with m.If(target_reads_sdram != 0xffff):
                        m.d.sdram += target_reads_sdram.eq(
                            target_reads_sdram + 1)
                    with m.If(~zero_boundary_seen_sdram):
                        with m.If(
                                target_reads_before_boundary_sdram !=
                                0xffff):
                            m.d.sdram += (
                                target_reads_before_boundary_sdram.eq(
                                    target_reads_before_boundary_sdram + 1))
                    with m.Elif(
                            arm_mode_sdram ==
                            SDRAMCaptureMode.DEEP_SELF_REFRESH):
                        m.d.sdram += [
                            target_seen_sdram.eq(1),
                            frozen_sdram.eq(1),
                        ]

                # Pipeline stage 1: classify the registered bus sample and
                # retain the active-row context with each READ record.
                m.d.sdram += [
                    record_q.eq(sample_q_record),
                    store_q.eq(
                        sample_valid_q &
                        Mux(
                            arm_mode_sdram ==
                            SDRAMCaptureMode.CALIBRATION_FILTERED,
                            sample_q_filtered_match,
                            1,
                        )),
                    zero_match_q.eq(
                        sample_valid_q & sample_q_zero_match),
                    target_match_q.eq(
                        sample_valid_q & sample_q_target_match),
                ]

                # Track only the profile-selected row bits observed by the
                # reduced probe.
                # A10/auto-precharge is not present, so awake A/B calibration
                # and strict NOR ordering remain mandatory.
                with m.If(sample_valid_q & sample_q_is_activate):
                    m.d.sdram += [
                        active_row[sample_q_bank].eq(sample_q_row),
                        active_valid[sample_q_bank].eq(1),
                    ]
                with m.Elif(
                        sample_valid_q &
                        (sample_q_is_precharge |
                         sample_q_is_auto_refresh |
                         sample_q_is_mode_register_set)):
                    for bank_index in range(4):
                        m.d.sdram += active_valid[bank_index].eq(0)

                # Pipeline stage 0: sample direct inputs. This stage is active
                # only after immediate trigger or self-refresh entry.
                with m.If(triggered_sdram):
                    m.d.sdram += [
                        sample_q.eq(self.sample),
                        sample_clock_index_q.eq(clock_edges_sdram[:16]),
                        sample_valid_q.eq(is_non_nop),
                    ]
                with m.Else():
                    m.d.sdram += [
                        sample_valid_q.eq(0),
                        store_q.eq(0),
                        zero_match_q.eq(0),
                        target_match_q.eq(0),
                    ]
                    with m.If(is_self_refresh_entry):
                        # With pin-local input flip-flops, this branch runs on
                        # the first later SDCLK1 edge. If no such edge arrives,
                        # ``last_sample`` still retains the self-refresh
                        # command for host-domain readback.
                        m.d.sdram += [
                            triggered_sdram.eq(1),
                            self_refresh_seen_sdram.eq(1),
                            trigger_sample_sdram.eq(self.sample),
                            trigger_clock_index_sdram.eq(
                                clock_edges_sdram[:16] - 1),
                        ]
                        for bank_index in range(4):
                            m.d.sdram += active_valid[bank_index].eq(0)

        def synchronize_status(name, source, target):
            m.submodules[name] = cdc.FFSynchronizer(source, target)

        synchronize_status(
            "armed_ack_to_sync", armed_ack_sdram, self.armed_ack)
        synchronize_status(
            "arm_generation_to_sync",
            arm_generation_sdram, self.arm_generation)
        synchronize_status(
            "arm_mode_to_sync", arm_mode_sdram, self.arm_mode)
        synchronize_status(
            "arm_zero_signature_to_sync",
            arm_zero_signature_sdram, self.arm_zero_signature)
        synchronize_status(
            "arm_target_signature_to_sync",
            arm_target_signature_sdram, self.arm_target_signature)
        synchronize_status(
            "arm_minimum_zero_reads_to_sync",
            arm_minimum_zero_reads_sdram, self.arm_minimum_zero_reads)
        synchronize_status(
            "triggered_to_sync", triggered_sdram, self.triggered)
        synchronize_status(
            "self_refresh_seen_to_sync",
            self_refresh_seen_sdram, self.self_refresh_seen)
        synchronize_status(
            "trigger_sample_to_sync",
            trigger_sample_sdram, self.trigger_sample)
        synchronize_status(
            "trigger_clock_index_to_sync",
            trigger_clock_index_sdram, self.trigger_clock_index)
        synchronize_status(
            "last_sample_to_sync", self.sample, self.last_sample)
        synchronize_status("frozen_to_sync", frozen_sdram, self.frozen)
        synchronize_status("overflow_to_sync", overflow_sdram, self.overflow)
        synchronize_status(
            "target_seen_to_sync", target_seen_sdram, self.target_seen)

        def synchronize_gray_counter(name, source, target):
            source_gray = Signal.like(source, name=f"{name}_source_gray")
            target_gray = Signal.like(source, name=f"{name}_target_gray")
            target_binary = Signal.like(source, name=f"{name}_target_binary")
            m.d.comb += source_gray.eq(source ^ (source >> 1))
            m.submodules[name] = cdc.FFSynchronizer(
                source_gray, target_gray)
            m.d.comb += target_binary[-1].eq(target_gray[-1])
            for bit in range(len(target_binary) - 2, -1, -1):
                m.d.comb += target_binary[bit].eq(
                    target_binary[bit + 1] ^ target_gray[bit])
            m.d.comb += target.eq(target_binary)

        synchronize_gray_counter(
            "event_count_to_sync", event_count_sdram, self.event_count)
        synchronize_gray_counter(
            "clock_edges_to_sync", clock_edges_sdram, self.clock_edges)
        synchronize_gray_counter(
            "zero_reads_to_sync", zero_reads_sdram, self.zero_reads)
        synchronize_gray_counter(
            "target_reads_to_sync", target_reads_sdram, self.target_reads)
        synchronize_gray_counter(
            "target_reads_before_boundary_to_sync",
            target_reads_before_boundary_sdram,
            self.target_reads_before_boundary)

        return m


class SDRAMResumeProbeComponent(wiring.Component):
    armed: In(1)
    freeze: In(1)
    generation: In(16)
    mode: In(2)
    zero_signature: In(8)
    target_signature: In(8)
    minimum_zero_reads: In(8)
    record_index: In(range(SDRAM_CAPTURE_DEPTH))

    input_only_mask: Out(13)
    armed_ack: Out(1)
    arm_generation: Out(16)
    arm_mode: Out(2)
    arm_zero_signature: Out(8)
    arm_target_signature: Out(8)
    arm_minimum_zero_reads: Out(8)
    triggered: Out(1)
    self_refresh_seen: Out(1)
    trigger_sample: Out(SAMPLE_WIDTH)
    trigger_clock_index: Out(16)
    last_sample: Out(SAMPLE_WIDTH)
    frozen: Out(1)
    overflow: Out(1)
    target_seen: Out(1)
    event_count: Out(range(SDRAM_CAPTURE_DEPTH + 1))
    clock_edges: Out(32)
    zero_reads: Out(16)
    target_reads: Out(16)
    target_reads_before_boundary: Out(16)
    record: Out(32)

    def __init__(self, ports, *, signal_profile="v9-a0018330"):
        self._ports = ports
        self.signal_profile = resolve_signal_profile(signal_profile)
        super().__init__()

    def elaborate(self, platform):
        m = Module()

        # Every observed net is permanently input-only. In particular, there
        # is no output-enable signal whose stale state could drive SDRAM.
        m.submodules.clk_buffer = clk_buffer = io.Buffer(
            "i", self._ports.clk)
        m.domains.sdram_io = cd_sdram_io = ClockDomain(
            reset_less=True, local=True)
        m.d.comb += cd_sdram_io.clk.eq(clk_buffer.i)
        m.submodules.cke_buffer = cke_buffer = io.FFBuffer(
            "i", self._ports.cke, i_domain="sdram_io")
        m.submodules.cs_buffer = cs_buffer = io.FFBuffer(
            "i", self._ports.cs, i_domain="sdram_io")
        m.submodules.ras_buffer = ras_buffer = io.FFBuffer(
            "i", self._ports.ras, i_domain="sdram_io")
        m.submodules.cas_buffer = cas_buffer = io.FFBuffer(
            "i", self._ports.cas, i_domain="sdram_io")
        m.submodules.we_buffer = we_buffer = io.FFBuffer(
            "i", self._ports.we, i_domain="sdram_io")
        m.submodules.ba_buffer = ba_buffer = io.FFBuffer(
            "i", self._ports.ba, i_domain="sdram_io")
        m.submodules.address_buffer = address_buffer = io.FFBuffer(
            "i", self._ports.address, i_domain="sdram_io")

        m.submodules.capture = capture = SDRAMResumeCapture(
            signal_profile=self.signal_profile)
        m.d.comb += [
            capture.armed.eq(self.armed),
            capture.freeze.eq(self.freeze),
            capture.generation.eq(self.generation),
            capture.mode.eq(self.mode),
            capture.zero_signature.eq(self.zero_signature),
            capture.target_signature.eq(self.target_signature),
            capture.minimum_zero_reads.eq(self.minimum_zero_reads),
            capture.clk_i.eq(clk_buffer.i),
            capture.sample.eq(Cat(
                cke_buffer.i,
                cs_buffer.i,
                ras_buffer.i,
                cas_buffer.i,
                we_buffer.i,
                ba_buffer.i,
                address_buffer.i,
            )),
            capture.record_index.eq(self.record_index),
            self.input_only_mask.eq(0x1fff),
            self.armed_ack.eq(capture.armed_ack),
            self.arm_generation.eq(capture.arm_generation),
            self.arm_mode.eq(capture.arm_mode),
            self.arm_zero_signature.eq(capture.arm_zero_signature),
            self.arm_target_signature.eq(capture.arm_target_signature),
            self.arm_minimum_zero_reads.eq(
                capture.arm_minimum_zero_reads),
            self.triggered.eq(capture.triggered),
            self.self_refresh_seen.eq(capture.self_refresh_seen),
            self.trigger_sample.eq(capture.trigger_sample),
            self.trigger_clock_index.eq(capture.trigger_clock_index),
            self.last_sample.eq(capture.last_sample),
            self.frozen.eq(capture.frozen),
            self.overflow.eq(capture.overflow),
            self.target_seen.eq(capture.target_seen),
            self.event_count.eq(capture.event_count),
            self.clock_edges.eq(capture.clock_edges),
            self.zero_reads.eq(capture.zero_reads),
            self.target_reads.eq(capture.target_reads),
            self.target_reads_before_boundary.eq(
                capture.target_reads_before_boundary),
            self.record.eq(capture.record),
        ]
        return m


class SDRAMResumeProbeInterface:
    def __init__(self, logger: logging.Logger, assembly: AbstractAssembly, *,
            clk: GlasgowPin, cke: GlasgowPin, cs: GlasgowPin,
            ras: GlasgowPin, cas: GlasgowPin, we: GlasgowPin,
            ba: tuple[GlasgowPin, ...],
            address: tuple[GlasgowPin, ...],
            signal_profile):
        self._logger = logger
        self.signal_profile = resolve_signal_profile(signal_profile)
        self.profile_metadata = self.signal_profile.metadata()
        ports = assembly.add_port_group(
            clk=clk, cke=cke, cs=cs, ras=ras, cas=cas, we=we,
            ba=ba, address=address)
        component = assembly.add_submodule(SDRAMResumeProbeComponent(
            ports, signal_profile=self.signal_profile))

        self._armed = assembly.add_rw_register(component.armed)
        self._freeze = assembly.add_rw_register(component.freeze)
        self._generation = assembly.add_rw_register(component.generation)
        self._mode = assembly.add_rw_register(component.mode)
        self._zero_signature = assembly.add_rw_register(
            component.zero_signature)
        self._target_signature = assembly.add_rw_register(
            component.target_signature)
        self._minimum_zero_reads = assembly.add_rw_register(
            component.minimum_zero_reads)
        self._record_index = assembly.add_rw_register(component.record_index)

        self._input_only_mask = assembly.add_ro_register(
            component.input_only_mask)
        self._armed_ack = assembly.add_ro_register(component.armed_ack)
        self._arm_generation = assembly.add_ro_register(
            component.arm_generation)
        self._arm_mode = assembly.add_ro_register(component.arm_mode)
        self._arm_zero_signature = assembly.add_ro_register(
            component.arm_zero_signature)
        self._arm_target_signature = assembly.add_ro_register(
            component.arm_target_signature)
        self._arm_minimum_zero_reads = assembly.add_ro_register(
            component.arm_minimum_zero_reads)
        self._triggered = assembly.add_ro_register(component.triggered)
        self._self_refresh_seen = assembly.add_ro_register(
            component.self_refresh_seen)
        self._trigger_sample = assembly.add_ro_register(
            component.trigger_sample)
        self._trigger_clock_index = assembly.add_ro_register(
            component.trigger_clock_index)
        self._last_sample = assembly.add_ro_register(component.last_sample)
        self._frozen = assembly.add_ro_register(component.frozen)
        self._overflow = assembly.add_ro_register(component.overflow)
        self._target_seen = assembly.add_ro_register(component.target_seen)
        self._event_count = assembly.add_ro_register(component.event_count)
        self._clock_edges = assembly.add_ro_register(component.clock_edges)
        self._zero_reads = assembly.add_ro_register(component.zero_reads)
        self._target_reads = assembly.add_ro_register(component.target_reads)
        self._target_reads_before_boundary = assembly.add_ro_register(
            component.target_reads_before_boundary)
        self._record = assembly.add_ro_register(component.record)

    async def status(self):
        last_sample = await self._last_sample
        return SDRAMCaptureStatus(
            armed=bool(await self._armed_ack),
            input_only_mask=await self._input_only_mask,
            generation=await self._arm_generation,
            mode=await self._arm_mode,
            zero_signature=await self._arm_zero_signature,
            target_signature=await self._arm_target_signature,
            minimum_zero_reads=await self._arm_minimum_zero_reads,
            triggered=bool(await self._triggered),
            self_refresh_seen=bool(await self._self_refresh_seen),
            trigger_sample=await self._trigger_sample,
            trigger_clock_index=await self._trigger_clock_index,
            last_sample=last_sample,
            last_sample_is_self_refresh=is_self_refresh_sample(last_sample),
            frozen=bool(await self._frozen),
            overflow=bool(await self._overflow),
            target_seen=bool(await self._target_seen),
            event_count=await self._event_count,
            clock_edges=await self._clock_edges,
            zero_reads=await self._zero_reads,
            target_reads=await self._target_reads,
            target_reads_before_boundary=(
                await self._target_reads_before_boundary),
        )

    async def arm(self, *, generation, mode, zero_signature=None,
            target_signature=None, minimum_zero_reads=8,
            address_profile, mapping_sha256,
            wiring_attested=False, loading_qualified=False,
            awake_calibration_attested=False):
        try:
            mode = SDRAMCaptureMode(mode)
        except ValueError as error:
            raise SDRAMResumeProbeError(
                f"unknown capture mode {mode!r}") from error
        validate_signal_profile_attestation(
            self.signal_profile, address_profile, mapping_sha256)
        zero_signature, target_signature = resolve_arm_signatures(
            self.signal_profile, zero_signature, target_signature)
        if not wiring_attested or not loading_qualified:
            raise SDRAMResumeProbeError(
                "arm requires explicit wiring and loading attestations")
        if (mode == SDRAMCaptureMode.DEEP_SELF_REFRESH and
                not awake_calibration_attested):
            raise SDRAMResumeProbeError(
                "deep arm requires an accepted awake A/B calibration")
        if not 0 < generation <= 0xffff:
            raise SDRAMResumeProbeError("generation must be in 1..65535")
        if not 0 <= zero_signature <= 0xff:
            raise SDRAMResumeProbeError("zero signature must fit in 8 bits")
        if not 0 <= target_signature <= 0xff:
            raise SDRAMResumeProbeError("target signature must fit in 8 bits")
        if zero_signature == target_signature:
            raise SDRAMResumeProbeError(
                "zero and target signatures must differ")
        if not 1 <= minimum_zero_reads <= 0xff:
            raise SDRAMResumeProbeError(
                "minimum zero reads must be in 1..255")
        if await self._armed:
            raise SDRAMResumeProbeError("probe is already armed")

        await self._freeze.set(0)
        await self._generation.set(generation)
        await self._mode.set(int(mode))
        await self._zero_signature.set(zero_signature)
        await self._target_signature.set(target_signature)
        await self._minimum_zero_reads.set(minimum_zero_reads)
        await self._armed.set(1)
        await asyncio.sleep(0.005)

        status = await self.status()
        if not status.armed:
            raise SDRAMResumeProbeError(
                "SDCLK1 did not acknowledge arm; it may not be running")
        expected = (
            generation, int(mode), zero_signature, target_signature,
            minimum_zero_reads)
        observed = (
            status.generation, status.mode, status.zero_signature,
            status.target_signature, status.minimum_zero_reads)
        if observed != expected:
            raise SDRAMResumeProbeError(
                f"arm configuration mismatch {observed!r} != {expected!r}")
        if status.input_only_mask != 0x1fff:
            raise SDRAMResumeProbeError(
                f"input-only readback is 0x{status.input_only_mask:x}")
        if (status.frozen or status.overflow or status.target_seen or
                status.event_count or status.zero_reads or
                status.target_reads or
                status.target_reads_before_boundary):
            raise SDRAMResumeProbeError(
                f"probe did not arm empty: {status!r}")
        if (mode == SDRAMCaptureMode.DEEP_SELF_REFRESH and
                status.triggered):
            raise SDRAMResumeProbeError(
                f"deep probe triggered while arming: {status!r}")
        if (mode != SDRAMCaptureMode.DEEP_SELF_REFRESH and
                not status.triggered):
            raise SDRAMResumeProbeError(
                f"immediate probe did not trigger: {status!r}")
        return status

    async def freeze(self):
        if not await self._armed:
            raise SDRAMResumeProbeError("probe is not armed")
        await self._freeze.set(1)
        for _ in range(20):
            await asyncio.sleep(0.005)
            status = await self.status()
            if status.frozen:
                return status
        raise SDRAMResumeProbeError(
            "SDCLK1 did not acknowledge freeze; if the target clock is "
            "stopped, drain with an explicit stopped-clock attestation")

    async def drain(self, *, stopped_clock_attested=False):
        status = await self.status()
        if not status.armed:
            raise SDRAMResumeProbeError("probe is not armed")
        if not status.frozen and not stopped_clock_attested:
            raise SDRAMResumeProbeError(
                "live capture drain requires frozen state or an explicit "
                "stopped-clock attestation")
        if status.event_count > SDRAM_CAPTURE_DEPTH:
            raise SDRAMResumeProbeError(
                f"impossible event count {status.event_count}")

        records = []
        if status.self_refresh_seen:
            trigger_record = (
                status.trigger_sample |
                (status.trigger_clock_index << 15))
            records.append({
                "index": 0,
                "source": "self-refresh-trigger-register",
                **decode_capture_record(
                    trigger_record, self.signal_profile),
            })
        elif (status.mode == SDRAMCaptureMode.DEEP_SELF_REFRESH and
                status.last_sample_is_self_refresh):
            trigger_record = (
                status.last_sample |
                (((status.clock_edges - 1) & 0xffff) << 15))
            records.append({
                "index": 0,
                "source": "stopped-clock-last-sample",
                **decode_capture_record(
                    trigger_record, self.signal_profile),
            })
        for index in range(status.event_count):
            await self._record_index.set(index)
            await asyncio.sleep(0)
            records.append({
                "index": len(records),
                "source": "capture-ram",
                "ram_index": index,
                **decode_capture_record(
                    await self._record, self.signal_profile),
            })
        report = {
            "schema": 2,
            "kind": "sl-c860-glasgow-sdram-resume-capture",
            "signal_profile": self.profile_metadata,
            "status": asdict(status),
            "record_layout": {
                "sample": "bits 11:0",
                "active_row_discriminator": "bits 13:12",
                "active_row_signals": (
                    self.profile_metadata[
                        "active_row_signature_order"]),
                "active_row_valid": "bit 14",
                "clock_index_modulo_65536": "bits 30:15",
                "reserved_zero": "bit 31",
            },
            "records": records,
            "accepted": False,
            "instruction": (
                "Retain and hash this raw report. Classification requires "
                "the separately reviewed awake A/B calibration."),
        }
        return status, report

    async def disarm(self):
        if not await self._armed:
            return await self.status()
        await self._armed.set(0)
        for _ in range(20):
            await asyncio.sleep(0.005)
            status = await self.status()
            if not status.armed:
                await self._freeze.set(0)
                return status
        raise SDRAMResumeProbeError(
            "SDCLK1 did not acknowledge disarm; power off the target and "
            "reload the input-only bitstream rather than discarding evidence")


class SDRAMResumeProbeApplet(GlasgowAppletV2):
    logger = logging.getLogger(__name__)
    help = "capture the SL-C860 SDRAM resume handoff"
    description = """
    Capture a reduced, source-synchronous PXA255 SDR SDRAM command/address
    trace for the SL-C860 NOR-to-PSPR resume handoff. The applet uses observed
    SDCLK1 as its capture clock and permanently instantiates all 13 probe nets
    as inputs.

    This applet is not an ordinary 48 MHz logic analyzer. It is qualified only
    after unit-specific board mapping, 3.3 V electrical review, passive loading
    tests, and awake zero/target A/B calibration. Its reduced address set is a
    discriminator for the calibrated pair, not a globally unique address
    decoder.
    """
    required_revision = "C3"
    preview = True

    @classmethod
    def add_build_arguments(cls, parser, access):
        access.add_voltage_argument(parser)
        parser.add_argument(
            "--signal-profile",
            choices=tuple(SDRAM_SIGNAL_PROFILES),
            required=True,
            help=(
                "explicit ordered address/signature profile; must match "
                "the prepared disposable kernel target"))
        access.add_pins_argument(
            parser, "clk", default="A1", required=True,
            help="observed PXA SDCLK1")
        access.add_pins_argument(
            parser, "cke", default="A0", required=True,
            help="observed PXA SDCKE1")
        access.add_pins_argument(
            parser, "cs", default="A2", required=True,
            help="observed active-low nSDCS0")
        access.add_pins_argument(
            parser, "ras", default="A3", required=True,
            help="observed active-low nSDRAS")
        access.add_pins_argument(
            parser, "cas", default="A4", required=True,
            help="observed active-low nSDCAS")
        access.add_pins_argument(
            parser, "we", default="A5", required=True,
            help="observed active-low nSDWE")
        access.add_pins_argument(
            parser, "ba", width=2, default="A6,A7", required=True,
            help="observed BA0,BA1")
        access.add_pins_argument(
            parser, "address", width=5, default="B0:4", required=True,
            help=(
                "five observed device address lines in --signal-profile "
                "order"))

    def build(self, args):
        with self.assembly.add_applet(self):
            self.assembly.use_voltage(args.voltage)
            self.sdram_probe_iface = SDRAMResumeProbeInterface(
                self.logger, self.assembly,
                clk=args.clk, cke=args.cke, cs=args.cs,
                ras=args.ras, cas=args.cas, we=args.we,
                ba=args.ba, address=args.address,
                signal_profile=args.signal_profile)
            self._signal_profile = resolve_signal_profile(
                args.signal_profile)
            self._signal_profile_metadata = (
                self._signal_profile.metadata())
            pins = (
                args.clk, args.cke, args.cs, args.ras, args.cas, args.we,
                *args.ba, *args.address)
            self._used_ports = {pin.port for pin in pins}

    @classmethod
    def add_setup_arguments(cls, parser):
        parser.add_argument(
            "--control", type=Path, required=True, metavar="SOCKET",
            help="serve newline-delimited JSON control on local Unix SOCKET")

    async def setup(self, args):
        if self.device.revision != "C3":
            raise SDRAMResumeProbeError(
                f"SDRAM resume probe is qualified only on revC3, not "
                f"{self.device.revision}")
        for port in self._used_ports:
            vio = args.voltage.get(port)
            if vio is None or vio.value != 3.3:
                raise SDRAMResumeProbeError(
                    f"SDRAM resume probe requires exact fixed -V {port}=3.3")
            actual = await self.device.get_voltage(str(port))
            if abs(actual - 3.3) > 0.1:
                raise SDRAMResumeProbeError(
                    f"port {port} VIO readback is {actual:.2f} V")
            await self.device.set_pulls(
                str(port), low=set(), high=set())
        if args.control.exists():
            raise SDRAMResumeProbeError(
                f"control socket already exists: {args.control}")

    async def run(self, args):
        async def client(reader, writer):
            try:
                while request_line := await reader.readline():
                    try:
                        request = json.loads(request_line)
                        if not isinstance(request, dict):
                            raise SDRAMResumeProbeError(
                                "request must be a JSON object")
                        command = request.get("command")
                        if command == "status":
                            response = {
                                "ok": True,
                                "signal_profile":
                                    self._signal_profile_metadata,
                                "status": asdict(
                                    await self.sdram_probe_iface.status()),
                            }
                        elif command == "arm":
                            mode_name = request.get("mode", "deep")
                            modes = {
                                "deep": SDRAMCaptureMode.DEEP_SELF_REFRESH,
                                "calibration":
                                    SDRAMCaptureMode.CALIBRATION_FILTERED,
                                "raw": SDRAMCaptureMode.RAW_IMMEDIATE,
                            }
                            if mode_name not in modes:
                                raise SDRAMResumeProbeError(
                                    f"unknown mode {mode_name!r}")
                            status = await self.sdram_probe_iface.arm(
                                generation=require_request_int(
                                    request, "generation"),
                                mode=modes[mode_name],
                                zero_signature=require_request_int(
                                    request, "zero_signature",
                                    default=self._signal_profile.zero_signature),
                                target_signature=require_request_int(
                                    request, "target_signature",
                                    default=(
                                        self._signal_profile.target_signature)),
                                minimum_zero_reads=require_request_int(
                                    request, "minimum_zero_reads", default=8),
                                address_profile=require_request_str(
                                    request, "address_profile"),
                                mapping_sha256=require_request_str(
                                    request, "mapping_sha256"),
                                wiring_attested=require_request_bool(
                                    request, "wiring_attested", default=False),
                                loading_qualified=require_request_bool(
                                    request, "loading_qualified",
                                    default=False),
                                awake_calibration_attested=(
                                    require_request_bool(
                                        request,
                                        "awake_calibration_attested",
                                        default=False)))
                            response = {
                                "ok": True,
                                "signal_profile":
                                    self._signal_profile_metadata,
                                "status": asdict(status)}
                        elif command == "freeze":
                            response = {
                                "ok": True,
                                "signal_profile":
                                    self._signal_profile_metadata,
                                "status": asdict(
                                    await self.sdram_probe_iface.freeze()),
                            }
                        elif command == "drain":
                            status, report = (
                                await self.sdram_probe_iface.drain(
                                    stopped_clock_attested=(
                                        require_request_bool(
                                            request,
                                            "stopped_clock_attested",
                                            default=False))))
                            response = {
                                "ok": True, "status": asdict(status),
                                "signal_profile":
                                    self._signal_profile_metadata,
                                "report": report,
                            }
                        elif command == "disarm":
                            response = {
                                "ok": True,
                                "signal_profile":
                                    self._signal_profile_metadata,
                                "status": asdict(
                                    await self.sdram_probe_iface.disarm()),
                            }
                        else:
                            raise SDRAMResumeProbeError(
                                f"unknown command {command!r}")
                    except BaseException as error:
                        response = {
                            "ok": False,
                            "error":
                                f"{error.__class__.__name__}: {error}",
                        }
                    writer.write(
                        json.dumps(response, sort_keys=True).encode() + b"\n")
                    await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        args.control.parent.mkdir(parents=True, exist_ok=True)
        server = await asyncio.start_unix_server(client, path=args.control)
        self.logger.info(
            "SDRAM resume-probe control listening at %s", args.control)
        try:
            async with server:
                await server.serve_forever()
        finally:
            try:
                args.control.unlink()
            except FileNotFoundError:
                pass

    @classmethod
    def tests(cls):
        from . import test
        return test.SDRAMResumeProbeAppletTestCase
