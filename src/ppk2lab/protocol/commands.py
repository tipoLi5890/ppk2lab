"""PPK2 command encoding.

Only opcodes exercised by the current official app path are exposed as stable
API (docs/protocol-spec.md section "Commands"). Legacy opcodes seen in older
public material are intentionally not exposed until a firmware-by-command
hardware test matrix proves them.
"""

from __future__ import annotations

import enum
import math
import struct

from ..errors import UsageError, VoltageRangeError
from ..types import VOLTAGE_MAX_MV, VOLTAGE_MIN_MV, Mode


class Opcode(enum.IntEnum):
    """Documented PPK2 opcodes used by the current official app path."""

    START_MEASURING = 0x06
    STOP_MEASURING = 0x07
    SET_DUT_POWER = 0x0C
    SET_SOURCE_VOLTAGE = 0x0D
    SET_MODE = 0x11
    GET_METADATA = 0x19
    RESET = 0x20
    SET_USER_GAIN = 0x25


def cmd_start_measuring() -> bytes:
    return bytes([Opcode.START_MEASURING])


def cmd_stop_measuring() -> bytes:
    return bytes([Opcode.STOP_MEASURING])


def cmd_set_dut_power(on: bool) -> bytes:
    return bytes([Opcode.SET_DUT_POWER, 1 if on else 0])


def cmd_set_source_voltage(voltage_mv: int) -> bytes:
    """Encode the source-voltage command (big-endian 16-bit millivolts).

    Voltage is validated against the device capability range before any bytes
    are produced; ambiguous or out-of-range values never reach the wire.
    """
    if not isinstance(voltage_mv, int):
        raise VoltageRangeError(
            f"voltage_mv must be an integer number of millivolts, got {voltage_mv!r}"
        )
    if not VOLTAGE_MIN_MV <= voltage_mv <= VOLTAGE_MAX_MV:
        raise VoltageRangeError(
            f"voltage_mv={voltage_mv} outside device range [{VOLTAGE_MIN_MV}, {VOLTAGE_MAX_MV}] mV"
        )
    return bytes([Opcode.SET_SOURCE_VOLTAGE, (voltage_mv >> 8) & 0xFF, voltage_mv & 0xFF])


def cmd_set_mode(mode: Mode) -> bytes:
    if not isinstance(mode, Mode):
        raise UsageError(f"mode must be Mode.AMPERE or Mode.SOURCE, got {mode!r}")
    return bytes([Opcode.SET_MODE, int(mode)])


def cmd_get_metadata() -> bytes:
    return bytes([Opcode.GET_METADATA])


def cmd_reset() -> bytes:
    return bytes([Opcode.RESET])


def cmd_set_user_gain(range_index: int, gain: float) -> bytes:
    """Encode the user-gain command for one measurement range.

    The float byte order has not yet been confirmed on hardware (see
    docs/protocol-spec.md "Unverified items"); little-endian IEEE 754 is used
    pending the hardware test matrix.
    """
    if not 0 <= range_index <= 4:
        raise UsageError(f"range_index must be 0-4, got {range_index}")
    if not math.isfinite(gain) or gain <= 0:
        raise UsageError(f"gain must be a positive finite number, got {gain!r}")
    return bytes([Opcode.SET_USER_GAIN, range_index]) + struct.pack("<f", gain)
