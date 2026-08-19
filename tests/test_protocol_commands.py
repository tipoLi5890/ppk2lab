import pytest

from ppk2lab.errors import UsageError, VoltageRangeError
from ppk2lab.protocol.commands import (
    cmd_set_dut_power,
    cmd_set_mode,
    cmd_set_source_voltage,
    cmd_set_user_gain,
    cmd_start_measuring,
    cmd_stop_measuring,
)
from ppk2lab.types import Mode


def test_basic_opcodes():
    assert cmd_start_measuring() == b"\x06"
    assert cmd_stop_measuring() == b"\x07"
    assert cmd_set_dut_power(True) == b"\x0c\x01"
    assert cmd_set_dut_power(False) == b"\x0c\x00"
    assert cmd_set_mode(Mode.AMPERE) == b"\x11\x01"
    assert cmd_set_mode(Mode.SOURCE) == b"\x11\x02"


def test_voltage_encoding_big_endian():
    assert cmd_set_source_voltage(3300) == bytes([0x0D, 3300 >> 8, 3300 & 0xFF])
    assert cmd_set_source_voltage(800) == bytes([0x0D, 0x03, 0x20])
    assert cmd_set_source_voltage(5000) == bytes([0x0D, 0x13, 0x88])


@pytest.mark.parametrize("mv", [799, 5001, 0, -100, 65536])
def test_voltage_range_refused(mv):
    with pytest.raises(VoltageRangeError):
        cmd_set_source_voltage(mv)


def test_voltage_rejects_non_integer():
    # Ambiguous bare values (e.g. 3.3 volts?) must never reach the wire.
    with pytest.raises(VoltageRangeError):
        cmd_set_source_voltage(3.3)  # type: ignore[arg-type]


def test_user_gain_validation():
    payload = cmd_set_user_gain(2, 1.0)
    assert payload[0] == 0x25
    assert payload[1] == 2
    assert len(payload) == 6
    with pytest.raises(UsageError):
        cmd_set_user_gain(5, 1.0)
    with pytest.raises(UsageError):
        cmd_set_user_gain(0, float("nan"))
