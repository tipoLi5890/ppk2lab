import math

import pytest

from ppk2lab.calibration import ADC_REFERENCE_FACTOR, Calibration, SpikeFilter
from ppk2lab.errors import Ppk2labError, UsageError
from ppk2lab.protocol.metadata import parse_metadata

from .test_metadata import FULL


def test_formula_matches_specification():
    cal = Calibration.from_metadata(parse_metadata(FULL))
    adc_field = 500
    r = 1
    # manual evaluation of the documented formula
    adc = adc_field * 4
    x = (adc - 100.0) * (ADC_REFERENCE_FACTOR / 101.5)
    expected = 1.0 * (x * (0.0 * x + 1.0) + 0.0 * 3.3 + 0.0) * 1e6  # amps -> uA
    assert cal.convert(adc_field, r) == pytest.approx(expected)


def test_nonlinear_and_vdd_terms():
    text = (
        FULL.replace("GS1: 0.0", "GS1: 0.5")
        .replace("S1: 0.0", "S1: 2.0")
        .replace("I1: 0.0", "I1: 7.0")
        .replace("UG1: 1.0", "UG1: 1.5")
    )
    cal = Calibration.from_metadata(parse_metadata(text))
    adc_field = 1000
    adc = adc_field * 4
    x = (adc - 100.0) * (ADC_REFERENCE_FACTOR / 101.5)
    expected = 1.5 * (x * (0.5 * x + 1.0) + 2.0 * 3.3 + 7.0) * 1e6  # amps -> uA
    assert cal.convert(adc_field, 1) == pytest.approx(expected)


def test_vdd_override():
    cal = Calibration.from_metadata(parse_metadata(FULL.replace("S1: 0.0", "S1: 1.0")))
    at_3300 = cal.convert(100, 1)
    at_5000 = cal.convert(100, 1, vdd_mv=5000)
    assert at_5000 - at_3300 == pytest.approx(1.0 * (5.0 - 3.3) * 1e6)


def test_missing_range_yields_nan_not_default():
    text = FULL.replace("R2: 10.2\n", "")
    cal = Calibration.from_metadata(parse_metadata(text))
    assert cal.missing_ranges() == [2]
    assert math.isnan(cal.convert(100, 2))
    assert not math.isnan(cal.convert(100, 1))


def test_invalid_range_index_is_nan():
    cal = Calibration.from_metadata(parse_metadata(FULL))
    assert math.isnan(cal.convert(100, 5))  # type: ignore[arg-type]


def test_unknown_vdd_yields_nan():
    text = FULL.replace("VDD: 3300\n", "")
    cal = Calibration.from_metadata(parse_metadata(text))
    assert math.isnan(cal.convert(100, 0))
    assert not math.isnan(cal.convert(100, 0, vdd_mv=3000))


def test_bad_construction_raises_the_documented_error_base():
    """`except Ppk2labError` is the documented contract for library errors, so
    a bare ValueError from a public constructor escapes every caller's
    handler."""
    with pytest.raises(UsageError) as excinfo:
        Calibration([None, None])
    assert isinstance(excinfo.value, Ppk2labError)
    assert excinfo.value.exit_code == 2
    with pytest.raises(UsageError):
        SpikeFilter(settle_samples=0)


def test_spike_filter_holds_after_range_switch():
    f = SpikeFilter(settle_samples=2)
    out = f.apply([1.0, 2.0, 100.0, 101.0, 102.0], bytes([0, 0, 1, 1, 1]))
    # the two samples after the 0->1 switch are held at the last stable value
    assert out == [1.0, 2.0, 2.0, 2.0, 102.0]


def test_spike_filter_resets_on_gap():
    f = SpikeFilter(settle_samples=2)
    f.apply([1.0, 2.0], bytes([0, 0]))
    f.notify_gap()
    # after a gap there is no held value; data passes through even though the
    # range changed relative to before the gap
    out = f.apply([50.0, 51.0], bytes([1, 1]))
    assert out == [50.0, 51.0]
