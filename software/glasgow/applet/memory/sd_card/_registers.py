from collections.abc import Buffer
from decimal import Decimal, InvalidOperation

from ._errors import SDCardError
from ._gateware_support import (
    BLOCK_SIZE, IMAGE_SIZE_GRANULARITY, MAX_IMAGE_SIZE,
)


__all__ = [
    "DEFAULT_TRANSFER_SPEED", "build_cid", "build_csd", "build_scr", "crc7", "crc16",
    "decode_csd_transfer_speed", "encode_csd_transfer_speed", "parse_transfer_speed",
]


DEFAULT_TRANSFER_SPEED = 25_000_000

# SD Physical Layer Specification 1.01, CSD TRAN_SPEED. Multipliers are represented in tenths
# so that every advertised rate is computed exactly without floating-point rounding.
_TRANSFER_SPEED_UNITS = (100_000, 1_000_000, 10_000_000, 100_000_000)
_TRANSFER_SPEED_MULTIPLIERS = (0, 10, 12, 13, 15, 20, 25, 30, 35,
                               40, 45, 50, 55, 60, 70, 80)
_TRANSFER_SPEED_CANDIDATES = tuple(sorted(
    (unit * multiplier // 10, (multiplier_code << 3) | unit_code)
    for unit_code, unit in enumerate(_TRANSFER_SPEED_UNITS)
    for multiplier_code, multiplier in enumerate(_TRANSFER_SPEED_MULTIPLIERS[1:], 1)
))


def decode_csd_transfer_speed(encoded: int) -> int:
    """Decode a CSD ``TRAN_SPEED`` byte into bits per second."""
    if not isinstance(encoded, int) or encoded not in range(0x80):
        raise SDCardError(f"invalid CSD transfer-speed value {encoded!r}")
    unit_code = encoded & 0x7
    multiplier_code = (encoded >> 3) & 0xf
    if unit_code >= len(_TRANSFER_SPEED_UNITS) or multiplier_code == 0:
        raise SDCardError(f"reserved CSD transfer-speed value {encoded:#04x}")
    return (_TRANSFER_SPEED_UNITS[unit_code] *
            _TRANSFER_SPEED_MULTIPLIERS[multiplier_code] // 10)


def encode_csd_transfer_speed(bits_per_second: int) -> int:
    """Encode the greatest representable CSD transfer speed at most *bits_per_second*."""
    if not isinstance(bits_per_second, int) or isinstance(bits_per_second, bool):
        raise SDCardError(f"transfer speed must be an integer, not {bits_per_second!r}")
    minimum = _TRANSFER_SPEED_CANDIDATES[0][0]
    maximum = _TRANSFER_SPEED_CANDIDATES[-1][0]
    if bits_per_second < minimum or bits_per_second > maximum:
        raise SDCardError(
            f"transfer speed must be between {minimum} and {maximum} bit/s")
    return max(candidate for candidate in _TRANSFER_SPEED_CANDIDATES
               if candidate[0] <= bits_per_second)[1]


def parse_transfer_speed(value: str) -> int:
    """Parse a command-line bit rate such as ``25m``, ``12.5mbit/s``, or ``500k``."""
    text = value.strip().lower().replace(" ", "")
    suffixes = (
        ("mbit/s", 1_000_000), ("mbps", 1_000_000), ("m", 1_000_000),
        ("kbit/s", 1_000), ("kbps", 1_000), ("k", 1_000),
        ("bit/s", 1), ("bps", 1),
    )
    multiplier = 1
    for suffix, candidate_multiplier in suffixes:
        if text.endswith(suffix):
            text = text[:-len(suffix)]
            multiplier = candidate_multiplier
            break
    try:
        rate = Decimal(text) * multiplier
    except InvalidOperation as error:
        raise ValueError(f"invalid transfer speed {value!r}") from error
    if not rate.is_finite() or rate != rate.to_integral_value():
        raise ValueError(f"transfer speed {value!r} is not a whole number of bit/s")
    rate = int(rate)
    try:
        encode_csd_transfer_speed(rate)
    except SDCardError as error:
        raise ValueError(str(error)) from error
    return rate


def _crc7_step(crc: int, bit: int) -> int:
    feedback = bit ^ ((crc >> 6) & 1)
    crc = (crc << 1) & 0x7f
    if feedback:
        crc ^= 0x09
    return crc


def crc7(value: int, width: int) -> int:
    """Return the SD CRC7 of *width* bits, processed most-significant bit first."""
    crc = 0
    for bitno in reversed(range(width)):
        crc = _crc7_step(crc, (value >> bitno) & 1)
    return crc


def _crc16_step(crc: int, bit: int) -> int:
    feedback = bit ^ ((crc >> 15) & 1)
    crc = (crc << 1) & 0xffff
    if feedback:
        crc ^= 0x1021
    return crc


def crc16(data: Buffer) -> int:
    """Return the SD CRC16-CCITT of *data*, processed most-significant bit first."""
    crc = 0
    for octet in data:
        for bitno in reversed(range(8)):
            crc = _crc16_step(crc, (octet >> bitno) & 1)
    return crc


def _set_field(value: int, high: int, low: int, field: int) -> int:
    width = high - low + 1
    assert field in range(1 << width)
    mask = ((1 << width) - 1) << low
    return (value & ~mask) | (field << low)


def _finish_register_crc(value: int) -> int:
    value = _set_field(value, 7, 1, crc7(value >> 8, 120))
    return _set_field(value, 0, 0, 1)


def build_cid(*, serial: int = 0x474c_4153) -> int:
    """Build a deterministic 128-bit CID register for the emulated card."""
    cid = 0
    cid = _set_field(cid, 127, 120, 0x03)
    cid = _set_field(cid, 119, 104, int.from_bytes(b"GL", "big"))
    cid = _set_field(cid, 103, 64, int.from_bytes(b"HOST ", "big"))
    cid = _set_field(cid, 63, 56, 0x10)
    cid = _set_field(cid, 55, 24, serial & 0xffff_ffff)
    cid = _set_field(cid, 19, 8, ((2026 - 2000) << 4) | 7)
    return _finish_register_crc(cid)


def _capacity_fields(size: int) -> tuple[int, int]:
    if size <= 0 or size > MAX_IMAGE_SIZE:
        raise SDCardError(
            f"image size must be between {IMAGE_SIZE_GRANULARITY} bytes and "
            f"{MAX_IMAGE_SIZE} bytes")
    if size % IMAGE_SIZE_GRANULARITY:
        raise SDCardError(
            f"image size must be a multiple of {IMAGE_SIZE_GRANULARITY} bytes")

    block_count = size // BLOCK_SIZE
    for c_size_mult in reversed(range(8)):
        multiplier = 1 << (c_size_mult + 2)
        if block_count % multiplier == 0:
            c_size = block_count // multiplier - 1
            if c_size in range(1 << 12):
                return c_size, c_size_mult
    raise SDCardError(f"image size {size} cannot be represented by a CSD v1 register")


def build_csd(size: int, *, read_only: bool = False,
        transfer_speed: int = DEFAULT_TRANSFER_SPEED) -> int:
    """Build a CSD v1 register describing a byte-addressed SDSC image."""
    c_size, c_size_mult = _capacity_fields(size)
    encoded_transfer_speed = encode_csd_transfer_speed(transfer_speed)

    csd = 0
    csd = _set_field(csd, 127, 126, 0)       # CSD_STRUCTURE v1
    csd = _set_field(csd, 119, 112, 0x7f)    # TAAC: 80 ms
    csd = _set_field(csd, 111, 104, 0xff)    # NSAC: 25,500 clocks
    csd = _set_field(csd, 103, 96, encoded_transfer_speed)  # TRAN_SPEED
    csd = _set_field(csd, 95, 84, 0x135)     # classes 0, 2, 4, 5, 8
    csd = _set_field(csd, 83, 80, 9)         # READ_BL_LEN: 512 bytes
    csd = _set_field(csd, 79, 79, 0)         # fixed-size reads only
    csd = _set_field(csd, 78, 78, 0)         # no write misalignment
    csd = _set_field(csd, 77, 77, 0)         # no read misalignment
    csd = _set_field(csd, 76, 76, 0)         # no DSR
    csd = _set_field(csd, 73, 62, c_size)
    csd = _set_field(csd, 49, 47, c_size_mult)
    csd = _set_field(csd, 46, 46, 1)         # single-block erase
    csd = _set_field(csd, 45, 39, 0)         # one write block per erase sector
    csd = _set_field(csd, 31, 31, 0)         # no write-protect groups
    csd = _set_field(csd, 28, 26, 7)         # conservative write-time factor
    csd = _set_field(csd, 25, 22, 9)         # WRITE_BL_LEN: 512 bytes
    csd = _set_field(csd, 21, 21, 0)         # fixed-size writes only
    csd = _set_field(csd, 15, 15, 0)
    csd = _set_field(csd, 12, 12, read_only)  # temporary write protection
    csd = _set_field(csd, 11, 10, 0)         # partitioned/DOS-style image
    return _finish_register_crc(csd)


def build_scr() -> int:
    """Build an SD 1.01 SCR supporting both 1-bit and 4-bit buses."""
    scr = 0
    scr = _set_field(scr, 63, 60, 0)         # SCR_STRUCTURE v1
    scr = _set_field(scr, 59, 56, 0)         # SD_SPEC 1.0/1.01
    scr = _set_field(scr, 55, 55, 1)         # erased data reads as ones
    scr = _set_field(scr, 54, 52, 0)         # no security protocol
    scr = _set_field(scr, 51, 48, 0b0101)    # 1-bit and 4-bit buses
    return scr
