"""Error bars: what the per-range figures mean and what they must not do.

An error bar that is wrong is worse than none, because it invites a decision.
The model is stated in docs/SPEC.md; these tests pin the three properties that
make it defensible — the per-range figures are systematic gain specifications
summed linearly, the resolution term is separate and required, and the
statistical spread of the mean is a different quantity carried under a
different name.

All vectors come from ppk2lab's own simulator and signal builders.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

import pytest

from ppk2lab.capture.stats import (
    RANGE_RESOLUTION_UA,
    RANGE_TYPICAL_ACCURACY,
    UNCERTAINTY_MODEL,
    compute_stats,
    format_with_uncertainty,
)
from ppk2lab.device import PPK2
from ppk2lab.protocol.samples import GapEvent
from ppk2lab.testing.profiles import ConstantProfile, DemoActivityProfile, StepProfile
from ppk2lab.transport.mock import MockTransport, SimulatedPPK2
from ppk2lab.types import SAMPLE_PERIOD_S

from .conftest import capture_of

CALIBRATION_DOC = Path(__file__).resolve().parents[1] / "docs" / "calibration.md"


def demo_capture(samples: int = 300_000):
    device = PPK2.open(
        transport=MockTransport(SimulatedPPK2(profile=DemoActivityProfile())), simulate=True
    )
    try:
        result = device.capture(sample_limit=samples, in_memory_limit_samples=None)
    finally:
        device.close()
    assert result.capture is not None
    return result


def all_currents(capture) -> list[float]:
    out: list[float] = []
    for event in capture.iter_events():
        if isinstance(event, GapEvent):
            continue
        out.extend(capture.calibration.convert_block(event, capture.source_voltage_mv))
    return out


# ---------------------------------------------------------------------------
# The systematic model


def test_the_published_constants_match_the_documented_range_tables():
    """The constants are a transcription of Nordic's typical per-range tables.
    If docs/calibration.md and the code disagree, one of them is lying about
    where the number came from."""
    if not CALIBRATION_DOC.exists():  # pragma: no cover - docs/ absent
        pytest.skip("docs/calibration.md is not present in this tree")
    rows = re.findall(
        r"^\|\s*(\d)\s*\|[^|]+\|\s*([0-9.]+)\s*uA\s*\|\s*±(\d+)%\s*\|$",
        CALIBRATION_DOC.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    assert len(rows) == 5, "the per-range table in docs/calibration.md did not parse"
    for index, resolution, accuracy in rows:
        assert RANGE_RESOLUTION_UA[int(index)] == float(resolution)
        assert RANGE_TYPICAL_ACCURACY[int(index)] == int(accuracy) / 100.0


def test_per_range_contributions_are_summed_linearly_not_in_quadrature():
    """Nordic's per-range figures are gain specifications, so within a range
    the error is the same error on every sample and across ranges the terms
    add. Combining them in quadrature would claim a cancellation between two
    systematic errors that has no physical basis — and would under-report."""
    # Comparable time in ranges 2 and 3, so the two contributions are of the
    # same order and the choice of combination rule actually matters.
    capture = capture_of(StepProfile([(1000, 4_000.0, 0), (1000, 6_000.0, 0)]), samples=2000)
    stats = compute_stats(capture)
    assert stats.samples_per_range[2] == stats.samples_per_range[3] == 1000
    terms = [
        RANGE_TYPICAL_ACCURACY[r] * abs(stats.charge_per_range_uc[r])
        + RANGE_RESOLUTION_UA[r] * stats.samples_per_range[r] * SAMPLE_PERIOD_S
        for r in range(5)
    ]
    assert stats.uncertainty.charge_uc == pytest.approx(sum(terms), rel=1e-12)
    quadrature = math.sqrt(sum(t * t for t in terms))
    assert quadrature < 0.8 * stats.uncertainty.charge_uc


def test_the_error_bar_does_not_shrink_with_capture_length():
    """A systematic error does not average away. Doubling the capture of a
    steady load must leave the relative uncertainty where it was; anything
    that fell by sqrt(2) would be treating a gain spec as random noise."""
    short = compute_stats(capture_of(ConstantProfile(300.0), samples=2_000))
    long = compute_stats(capture_of(ConstantProfile(300.0), samples=20_000))
    short_rel = short.uncertainty.mean_ua / short.mean_ua
    long_rel = long.uncertainty.mean_ua / long.mean_ua
    assert long_rel == pytest.approx(short_rel, rel=1e-6)
    # And the charge bar grows with the charge rather than falling behind it.
    assert long.uncertainty.charge_uc == pytest.approx(10 * short.uncertainty.charge_uc, rel=1e-3)


def test_the_resolution_term_dominates_at_the_bottom_of_a_range():
    """A purely multiplicative model would call a 1 uA sleep current +/-10%.
    Range 0's step is 0.2 uA, three times that, so the honest figure is +/-30%
    — and this is exactly the regime a low-power measurement lives in."""
    stats = compute_stats(capture_of(ConstantProfile(1.0), samples=2000))
    assert stats.uncertainty.mean_ua / stats.mean_ua == pytest.approx(0.30, abs=0.005)


@pytest.mark.parametrize(
    ("load_ua", "expected_relative"),
    [
        (5_000.0, 0.101),  # top of range 2: the gain term dominates
        (6_000.0, 0.108),  # just inside range 3: the 50 uA step is visible
        (40_000.0, 0.101),  # near the top of range 3: it is not any more
    ],
)
def test_relative_uncertainty_is_not_flat_across_a_range(load_ua, expected_relative):
    """Relative uncertainty rises toward the bottom of every range. A model
    that reported a flat 10% everywhere would hide exactly that."""
    stats = compute_stats(capture_of(ConstantProfile(load_ua), samples=2000))
    assert stats.uncertainty.mean_ua / stats.mean_ua == pytest.approx(expected_relative, abs=0.002)


def test_the_top_range_carries_its_own_wider_figure():
    """Range 4 is specified at +/-15%, not +/-10%; a single project-wide
    constant would understate every high-current measurement."""
    stats = compute_stats(capture_of(ConstantProfile(500_000.0), samples=2000))
    assert stats.samples_per_range[4] == stats.valid_samples
    assert stats.uncertainty.mean_ua / stats.mean_ua == pytest.approx(0.152, abs=0.002)


def test_a_mixed_load_lands_between_its_two_range_figures():
    """The bar is a decomposition of the same per-range charge already
    reported, so it must be reproducible from the published fields."""
    stats = compute_stats(demo_capture().capture)
    assert stats.uncertainty.mean_ua == pytest.approx(
        stats.uncertainty.charge_uc / (stats.valid_samples * SAMPLE_PERIOD_S), rel=1e-12
    )
    assert 0.10 < stats.uncertainty.mean_ua / stats.mean_ua < 0.15


# ---------------------------------------------------------------------------
# Typical, not guaranteed


def test_the_figures_are_published_as_typical_and_never_as_limits():
    """Nordic publishes these as typical values. Presenting them as guaranteed
    would be a claim this project cannot source."""
    payload = compute_stats(capture_of(ConstantProfile(100.0), samples=500)).to_json()
    assert payload["uncertainty"]["guaranteed"] is False
    assert payload["uncertainty"]["model"] == UNCERTAINTY_MODEL
    assert set(payload["uncertainty"]) == {
        "model",
        "guaranteed",
        "mean_ua_typical",
        "charge_uc_typical",
        "energy_uj_typical",
        "mean_ua_batch_stderr",
        "batch_count",
        "batch_samples",
    }


def test_no_error_bar_is_reported_when_nothing_was_convertible():
    """An error bar on nothing is not a tighter error bar."""
    capture = capture_of(StepProfile([(100, 10.0, 0)]), samples=100)
    capture.meta.metadata_text = capture.meta.metadata_text.replace("R0:", "RX:")
    capture._calibration = None
    stats = compute_stats(capture)
    assert stats.valid_samples == 0
    assert stats.uncertainty is None
    assert stats.to_json()["uncertainty"] is None


def test_energy_carries_only_the_current_sides_uncertainty():
    """Energy is charge times an assumed voltage the PPK2 never measures, so
    the assumption has no error bar to propagate. The energy figure scales
    exactly with the charge figure and nothing else."""
    stats = compute_stats(demo_capture(50_000).capture)
    assert stats.energy_uj is not None
    assert stats.uncertainty.energy_uj / stats.energy_uj == pytest.approx(
        stats.uncertainty.charge_uc / stats.charge_uc, rel=1e-12
    )


# ---------------------------------------------------------------------------
# The statistical bar is a different quantity


def test_the_statistical_bar_is_not_sigma_over_root_n():
    """10 us samples through a shunt-switching front end are not independent
    draws: the demo profile holds one level for 1500 of them at a time. The
    naive sigma/sqrt(N) is an order of magnitude too small, and reporting it
    would present a duty-cycled measurement as far more settled than it is."""
    result = demo_capture()
    stats = result.stats
    currents = all_currents(result.capture)
    naive = math.sqrt(
        sum((v - stats.mean_ua) ** 2 for v in currents) / (len(currents) - 1)
    ) / math.sqrt(len(currents))
    assert naive < 10.0
    assert stats.uncertainty.mean_ua_batch_stderr > 10 * naive


def test_the_statistical_and_systematic_bars_are_reported_separately():
    """They answer different questions — "is the instrument reading true" and
    "would this mean move if the window moved" — and adding them together
    would produce a number that answers neither."""
    stats = demo_capture(100_000).stats
    unc = stats.uncertainty
    assert unc.mean_ua > 0
    assert unc.mean_ua_batch_stderr > 0
    assert unc.mean_ua != unc.mean_ua_batch_stderr
    assert unc.batch_count >= 20


def test_a_steady_load_has_a_small_statistical_bar_and_the_same_systematic_one():
    """The false-alarm guard for the pair above: on a flat load the mean is
    highly repeatable, so the statistical bar nearly vanishes while the
    instrument's own accuracy is unchanged."""
    stats = compute_stats(capture_of(ConstantProfile(300.0), samples=50_000))
    assert stats.uncertainty.mean_ua_batch_stderr < 0.001 * stats.mean_ua
    assert stats.uncertainty.mean_ua == pytest.approx(0.10 * 300.0 + 0.5, rel=0.01)


def test_a_window_too_short_to_batch_reports_no_statistical_bar():
    """One batch has no spread. Reporting zero would claim a perfectly
    repeatable mean from a single observation of it."""
    stats = compute_stats(capture_of(ConstantProfile(100.0), samples=200))
    assert stats.uncertainty.batch_count == 1
    assert stats.uncertainty.mean_ua_batch_stderr is None
    assert stats.uncertainty.mean_ua is not None


# ---------------------------------------------------------------------------
# Reporting precision


@pytest.mark.parametrize(
    ("value", "uncertainty", "expected"),
    [
        (1806.719544039553, 188.34195440394183, "1810 +/- 190"),
        (142.0, 14.7, "142 +/- 15"),
        (6.0001220777635345, 0.8000122077763519, "6.00 +/- 0.80"),
        (0.1007, 0.21007, "0.10 +/- 0.21"),
    ],
)
def test_human_output_is_rounded_to_the_digits_the_interval_supports(value, uncertainty, expected):
    """`1806.719544039553 +/- 188` claims thirteen digits of a number whose
    third is already unknown."""
    assert format_with_uncertainty(value, uncertainty) == expected


def test_human_output_falls_back_to_the_plain_value_without_an_interval():
    """No interval means no basis for rounding; the value is not silently
    truncated."""
    assert format_with_uncertainty(1806.72, None) == "1806.72"
    assert format_with_uncertainty(1806.72, 0.0) == "1806.72"
    assert format_with_uncertainty(None, 1.0) == "n/a"


def test_json_keeps_full_precision():
    """The rounding is a presentation choice. A consumer that wants to do its
    own arithmetic gets the unrounded values."""
    stats = demo_capture(50_000).stats
    payload = stats.to_json()
    assert payload["current_ua"]["mean"] == stats.mean_ua
    assert payload["uncertainty"]["mean_ua_typical"] == stats.uncertainty.mean_ua
    assert repr(payload["uncertainty"]["mean_ua_typical"]) != repr(round(stats.uncertainty.mean_ua))
