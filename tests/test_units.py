import pytest

from ppk2lab.errors import UsageError
from ppk2lab.units import (
    parse_channel,
    parse_channel_set,
    parse_charge_uc,
    parse_current_ua,
    parse_duration_s,
    parse_energy_uj,
)


def test_duration_units():
    assert parse_duration_s("5s") == 5.0
    assert parse_duration_s("200ms") == pytest.approx(0.2)
    assert parse_duration_s("100us") == pytest.approx(1e-4)
    assert parse_duration_s("1.5min") == 90.0
    assert parse_duration_s("2") == 2.0  # bare seconds allowed for durations


def test_duration_hours():
    """Hours are a first-class unit: the soak measurements are stated in them."""
    assert parse_duration_s("1h") == 3600.0
    assert parse_duration_s("8h") == 28800.0
    assert parse_duration_s("24h") == 86400.0
    assert parse_duration_s("0.5h") == 1800.0
    assert parse_duration_s(" 8 h ") == 28800.0
    # The unit is the suffix, not a prefix of a longer word.
    assert parse_duration_s("8h") == parse_duration_s("28800s")


@pytest.mark.parametrize("bad", ["", "abc", "-1s", "0s", "5 hours", "8hr", "8H8", "h"])
def test_duration_rejects_invalid(bad):
    with pytest.raises(UsageError):
        parse_duration_s(bad)


def test_current_units():
    assert parse_current_ua("10uA") == 10.0
    assert parse_current_ua("10µA") == 10.0
    assert parse_current_ua("1.5mA") == 1500.0
    assert parse_current_ua("2A") == 2e6
    assert parse_current_ua("500nA") == pytest.approx(0.5)


def test_current_requires_unit():
    # Bare numbers are ambiguous and must be rejected by the safety contract.
    with pytest.raises(UsageError):
        parse_current_ua("10")
    with pytest.raises(UsageError):
        parse_current_ua("10V")


def test_charge_and_energy_units():
    assert parse_charge_uc("5uC") == 5.0
    assert parse_charge_uc("1mC") == 1000.0
    assert parse_energy_uj("100uJ") == 100.0
    assert parse_energy_uj("2J") == 2e6


def test_channel_parsing():
    assert parse_channel("D0") == 0
    assert parse_channel("d7") == 7
    assert parse_channel_set("D0-D7") == [0, 1, 2, 3, 4, 5, 6, 7]
    assert parse_channel_set("D1,D3") == [1, 3]
    assert parse_channel_set("D2-D4,D2") == [2, 3, 4]


@pytest.mark.parametrize("bad", ["D8", "X1", "", "D3-D1"])
def test_channel_rejects_invalid(bad):
    with pytest.raises(UsageError):
        parse_channel_set(bad)
