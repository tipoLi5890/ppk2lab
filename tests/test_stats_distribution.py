"""Distribution statistics: what a duty-cycled DUT actually draws.

A mean is the right answer to "how fast does this drain a battery" and the
wrong answer to "what is my sleep current". On the shipped demo profile the
true load is 6 uA for 85% of the time and 12 mA for the other 15%, and the
mean is 1806.72 uA — a current the DUT never draws for a single sample. These
tests pin the statistics that answer the second question, and the properties
that keep them honest: the grid never invents a value it did not observe, the
same window measured live and offline agrees, and a duty-cycle split always
carries the threshold that produced it.

All vectors come from ppk2lab's own simulator and signal builders.
"""

from __future__ import annotations

import math
from array import array

import pytest

from ppk2lab.capture.stats import (
    QUANTILE_BIN_COUNT,
    QUANTILE_BINS_PER_DECADE,
    QUANTILE_HALF_WIDTH_FRACTION,
    QUANTILE_MAX_UA,
    QUANTILE_MIN_UA,
    StatsAccumulator,
    compute_stats,
)
from ppk2lab.device import PPK2
from ppk2lab.protocol.samples import GapEvent, SampleBlock, pack_sample
from ppk2lab.testing.profiles import ConstantProfile, DemoActivityProfile, StepProfile
from ppk2lab.transport.mock import MockTransport, SimulatedPPK2

from .conftest import capture_of


def demo_capture(samples: int = 300_000):
    """Three seconds of the ``--simulate`` profile, captured through the driver."""
    device = PPK2.open(
        transport=MockTransport(SimulatedPPK2(profile=DemoActivityProfile())), simulate=True
    )
    try:
        result = device.capture(sample_limit=samples, in_memory_limit_samples=None)
    finally:
        device.close()
    assert result.capture is not None
    return result


def exact_quantile(values: list[float], q: float) -> float:
    """Inverse-CDF quantile straight from the samples, for cross-checking."""
    ordered = sorted(values)
    return ordered[max(1, math.ceil(q * len(ordered))) - 1]


def all_currents(capture) -> list[float]:
    out: list[float] = []
    for event in capture.iter_events():
        if isinstance(event, GapEvent):
            continue
        out.extend(capture.calibration.convert_block(event, capture.source_voltage_mv))
    return out


# ---------------------------------------------------------------------------
# The question the mean cannot answer


def test_the_median_names_a_current_the_dut_actually_draws():
    """On a duty-cycled load the mean sits in a gap in the distribution.

    The demo profile draws 6 uA or 12 mA and nothing between them. The mean of
    1806.72 uA is arithmetically correct and describes no state the DUT is ever
    in; the median lands on the sleep current, which is the number a low-power
    engineer is actually asking for.
    """
    stats = demo_capture().stats
    assert stats.mean_ua == pytest.approx(1806.72, abs=0.01)
    assert stats.p50_ua == pytest.approx(6.0, rel=QUANTILE_HALF_WIDTH_FRACTION)
    # Nothing in the capture is anywhere near the mean.
    currents = all_currents(demo_capture().capture)
    assert min(abs(v - stats.mean_ua) for v in currents) > 0.5 * stats.mean_ua


def test_the_upper_quantiles_find_the_active_state():
    """85% sleep / 15% burst means p90 and above sit on the burst current."""
    stats = demo_capture().stats
    assert stats.p90_ua == pytest.approx(12_000.0, rel=QUANTILE_HALF_WIDTH_FRACTION)
    assert stats.p99_ua == pytest.approx(12_000.0, rel=QUANTILE_HALF_WIDTH_FRACTION)
    assert stats.p999_ua == pytest.approx(12_000.0, rel=QUANTILE_HALF_WIDTH_FRACTION)


def test_quantiles_are_monotone_and_inside_the_observed_range():
    """A quantile can never fall outside what was measured, whatever the grid
    says: the reported value is clamped into [min, max] for exactly that
    reason."""
    stats = demo_capture().stats
    quantiles = [stats.p50_ua, stats.p90_ua, stats.p99_ua, stats.p999_ua]
    assert quantiles == sorted(quantiles)
    for value in quantiles:
        assert stats.min_ua <= value <= stats.max_ua


@pytest.mark.parametrize("q", [0.5, 0.9, 0.99])
def test_the_grid_quantizes_a_quantile_by_no_more_than_its_half_width(q):
    """The only error a histogram quantile carries is the width of its bin.

    Pinned against the exact quantile of the same samples: a staircase load
    puts values all over the grid, and every reported quantile must sit within
    one half-width of the truth.
    """
    capture = capture_of(
        StepProfile([(400, 3.0, 0), (400, 47.0, 0), (400, 900.0, 0), (400, 33_000.0, 0)]),
        samples=1600,
    )
    stats = compute_stats(capture)
    reported = {0.5: stats.p50_ua, 0.9: stats.p90_ua, 0.99: stats.p99_ua}[q]
    truth = exact_quantile(all_currents(capture), q)
    assert reported == pytest.approx(truth, rel=QUANTILE_HALF_WIDTH_FRACTION)


def test_the_grid_half_width_stays_well_under_the_instruments_own_accuracy():
    """The grid must never be the term that decides whether a quantile is
    usable. Nordic's typical accuracy is 10%; the grid contributes 0.9%."""
    assert 0.00903 < QUANTILE_HALF_WIDTH_FRACTION < 0.00905
    assert QUANTILE_HALF_WIDTH_FRACTION * 10 < 0.10


# ---------------------------------------------------------------------------
# Bounded state, whatever the capture length


def test_the_grid_costs_the_same_memory_at_every_capture_length():
    """The accumulator also runs live during an 8-hour capture, so it must not
    keep samples. Its distribution state is a fixed-size array."""
    short = StatsAccumulator(0)
    long = StatsAccumulator(0)
    small = capture_of(ConstantProfile(120.0), samples=500)
    big = capture_of(ConstantProfile(120.0), samples=50_000)
    for accumulator, capture in ((short, small), (long, big)):
        for event in capture.iter_events():
            if isinstance(event, GapEvent):
                continue
            accumulator.add_block(
                event, capture.calibration.convert_block(event, capture.source_voltage_mv)
            )
    assert len(short.bins) == len(long.bins)
    assert long.valid == 100 * short.valid
    # Batch state is bounded too: the list is halved rather than grown.
    assert len(long._batches) < 2 * 20


def test_a_quantile_is_never_reported_from_a_bin_index_off_the_end():
    """Every value the accumulator lets through is bounded by
    IMPLAUSIBLE_CURRENT_UA, and the grid runs that far, so the hot loop can
    index without a ceiling test. Pin the arithmetic that makes that safe."""
    accumulator = StatsAccumulator(0)
    words = array(
        "I", [pack_sample(adc=0x3FFF, range_index=4, counter=i, logic=0) for i in range(4)]
    )
    accumulator.add_block(SampleBlock(0, words), [1_000_000.0, 1_099_999.0, 999_999.0, 1.0])
    assert accumulator.valid == 4
    assert sum(accumulator.bins) == 4
    # Only the reading past the grid's top bin counts as above the instrument's
    # own range; 1.000 A itself sits in the last bin, which straddles 1 A.
    assert sum(accumulator.bins[QUANTILE_BIN_COUNT + 1 :]) == 1


# ---------------------------------------------------------------------------
# Values the grid cannot place


def test_a_load_under_the_grid_floor_reports_its_own_minimum():
    """Below 200 nA the instrument is at its own resolution limit and the log
    grid has no bin. The quantile is reported as an upper bound clamped to the
    observed minimum — never as the grid floor, which the DUT never drew."""
    capture = capture_of(ConstantProfile(0.1), samples=2000)
    stats = compute_stats(capture)
    assert stats.below_grid_samples == stats.valid_samples
    assert stats.p50_ua == stats.min_ua
    assert stats.p50_ua < QUANTILE_MIN_UA


def test_a_zero_reading_is_binned_rather_than_crashing_the_logarithm():
    """log(0) is undefined and log(negative) raises. A shunt reading at or
    below zero is a real observation, so the loop places it in the underflow
    bin instead of excluding it or failing."""
    accumulator = StatsAccumulator(0)
    words = array("I", [pack_sample(adc=0, range_index=0, counter=i, logic=0) for i in range(3)])
    accumulator.add_block(SampleBlock(0, words), [0.0, -1.5, 4.0])
    assert accumulator.valid == 3
    assert accumulator.bins[0] == 2
    # The true median is 0.0. A quantile served from the underflow bin is
    # reported at the grid floor, which is an upper bound on it — never below
    # what was observed, and never a fabricated interior value.
    assert accumulator.quantile(0.5) == QUANTILE_MIN_UA
    assert accumulator.quantile(0.5) >= accumulator.min_v
    assert accumulator.quantile(1.0) == pytest.approx(4.0, rel=QUANTILE_HALF_WIDTH_FRACTION)


def test_quantiles_describe_the_valid_samples_only():
    """A sample excluded from the mean must be excluded from the distribution
    too, or the two statistics stop describing the same set."""
    capture = capture_of(StepProfile([(100, 10.0, 0)]), samples=100)
    capture.meta.metadata_text = capture.meta.metadata_text.replace("R0:", "RX:")
    capture._calibration = None
    stats = compute_stats(capture)
    assert stats.nan_samples == 100
    assert stats.valid_samples == 0
    assert stats.p50_ua is None
    assert stats.below_grid_samples == 0


# ---------------------------------------------------------------------------
# The live path and the offline path must agree


def test_measuring_a_stored_capture_reproduces_what_the_capture_recorded():
    """The distribution is collected unconditionally in the one loop both paths
    run, so a stored capture's own statistics and a later `measure` of the same
    window cannot disagree — including the batch structure behind the
    statistical error bar, which must not depend on how the samples arrived."""
    result = demo_capture()
    assert result.stats.to_json() == compute_stats(result.capture).to_json()


def test_asking_for_a_duty_cycle_split_does_not_move_any_other_number():
    """`--state-threshold` adds a decomposition; it must not change the mean,
    the quantiles, or anything else a caller compares across runs."""
    capture = demo_capture().capture
    plain = compute_stats(capture).to_json()
    split = compute_stats(capture, state_threshold_ua=1000.0).to_json()
    assert split.pop("state_split") is not None
    plain.pop("state_split")
    assert plain == split


# ---------------------------------------------------------------------------
# Duty-cycle decomposition


def test_no_threshold_means_no_split_rather_than_an_empty_one():
    """The split is a function of a threshold. With no threshold there is no
    split to report, and reporting an empty one would imply a state boundary
    nobody chose."""
    assert compute_stats(demo_capture(50_000).capture).state_split is None


def test_the_split_carries_the_threshold_that_produced_it():
    """The same capture split at two thresholds gives two different, both
    correct, answers — so a split without its threshold is unreadable."""
    capture = demo_capture(100_000).capture
    low = compute_stats(capture, state_threshold_ua=10.0).state_split
    high = compute_stats(capture, state_threshold_ua=1000.0).state_split
    assert low.threshold_ua == 10.0
    assert high.threshold_ua == 1000.0
    # The UART bytes at 25 uA are "above" at 10 uA and "below" at 1000 uA.
    assert low.above.samples > high.above.samples


def test_the_split_partitions_the_window_exactly():
    """Time and charge are decomposed, not recomputed: the two states must add
    back up to the totals reported alongside them."""
    stats = compute_stats(demo_capture().capture, state_threshold_ua=1000.0)
    split = stats.state_split
    assert split.below.samples + split.above.samples == stats.valid_samples
    assert split.below.charge_uc + split.above.charge_uc == pytest.approx(
        stats.charge_uc, rel=1e-12
    )
    assert split.below.duration_s + split.above.duration_s == pytest.approx(
        stats.valid_samples * 1e-5
    )


def test_excursions_count_the_activity_bursts():
    """The demo profile runs one 15 ms burst per 100 ms cycle, so three seconds
    contain thirty above-threshold runs."""
    split = compute_stats(demo_capture().capture, state_threshold_ua=1000.0).state_split
    assert split.above.runs == 30
    assert split.below.runs == 30
    assert split.above.mean_ua == pytest.approx(12_000.0, rel=1e-3)
    assert split.below.mean_ua < 30.0


def test_a_gap_ends_a_run_instead_of_bridging_it():
    """Two samples separated by missing data are not consecutive. Claiming one
    continuous excursion across a gap would assert the DUT stayed high through
    samples that were never observed."""
    capture = capture_of(
        StepProfile([(200, 20_000.0, 0), (200, 20_000.0, 0)]), samples=400, gaps={200: 30}
    )
    split = compute_stats(capture, state_threshold_ua=1000.0).state_split
    # The simulator drops the 30 samples inside the gap, so 370 are stored.
    assert split.above.samples == 370
    assert split.above.runs == 2

    contiguous = capture_of(StepProfile([(400, 20_000.0, 0)]), samples=400)
    assert compute_stats(contiguous, state_threshold_ua=1000.0).state_split.above.runs == 1


def test_a_window_entirely_on_one_side_reports_one_run_and_no_other():
    """False-alarm guard: a flat load must not manufacture excursions."""
    capture = capture_of(ConstantProfile(5.0), samples=2000)
    split = compute_stats(capture, state_threshold_ua=1000.0).state_split
    assert split.above.samples == 0
    assert split.above.runs == 0
    assert split.above.mean_ua is None
    assert split.below.runs == 1


def test_the_threshold_boundary_is_inclusive_above():
    """A sample exactly on the threshold counts as above it, once, so the two
    states never both claim it and never both disown it."""
    accumulator = StatsAccumulator(0, state_threshold_ua=10.0)
    words = array("I", [pack_sample(adc=1, range_index=0, counter=i, logic=0) for i in range(3)])
    accumulator.add_block(SampleBlock(0, words), [9.999, 10.0, 10.001])
    split = accumulator.state_split()
    assert (split.below.samples, split.above.samples) == (1, 2)


# ---------------------------------------------------------------------------
# Published grid metadata


def test_the_result_publishes_the_grid_it_used():
    """A consumer comparing quantiles across releases needs to know the grid
    they were quantized to; it is reported rather than left implicit."""
    payload = compute_stats(demo_capture(20_000).capture).to_json()
    assert payload["distribution"] == {
        "grid_min_ua": QUANTILE_MIN_UA,
        "grid_max_ua": QUANTILE_MAX_UA,
        "bins_per_decade": QUANTILE_BINS_PER_DECADE,
        "quantile_half_width_fraction": QUANTILE_HALF_WIDTH_FRACTION,
        "below_grid_samples": 0,
        "above_grid_samples": 0,
        "quantiles_at_floor": [],
    }
    assert set(payload["current_ua"]) == {
        "mean",
        "min",
        "max",
        "peak_index",
        "p50",
        "p90",
        "p99",
        "p999",
    }


def test_a_distribution_straddling_the_floor_says_which_quantiles_are_bounds():
    """The real case: most samples under the floor, peaks above it.

    Measured on an unloaded PPK2, 69% of samples read below 200 nA while the
    maximum reads above it. The [min, max] clamp cannot help there — the
    maximum is above the floor — so the quantile *is* the floor, and saying so
    is the only thing that keeps it from reading as a measurement.
    """
    # Two thirds under the floor, one third above: p50 lands in the underflow
    # bin, p90 and p99 do not.
    accumulator = StatsAccumulator(0)
    n = 300
    words = array(
        "I", [pack_sample(adc=0, range_index=0, counter=i % 64, logic=0) for i in range(n)]
    )
    currents = [0.05] * 200 + [0.5] * 100
    accumulator.add_block(SampleBlock(0, words), currents)
    stats = accumulator.finalize()

    assert stats.below_grid_samples == 200
    assert stats.quantiles_at_floor == ["p50"]
    assert stats.p50_ua == QUANTILE_MIN_UA  # the floor, not the observed 0.05
    assert stats.p90_ua > QUANTILE_MIN_UA  # measured, not bounded

    codes = {d.code for d in stats.diagnostics()}
    assert "W_BELOW_MEASUREMENT_FLOOR" in codes
    message = next(d.message for d in stats.diagnostics() if d.code == "W_BELOW_MEASUREMENT_FLOOR")
    assert "66.7%" in message and "p50 reports" in message

    published = stats.to_json()["distribution"]["quantiles_at_floor"]
    assert published == ["p50"]


def test_a_load_clear_of_the_floor_raises_no_bound_warning():
    """False-alarm guard: the ordinary case must stay silent."""
    stats = compute_stats(capture_of(ConstantProfile(1000.0), samples=2000))
    assert stats.below_grid_samples == 0
    assert stats.quantiles_at_floor == []
    assert "W_BELOW_MEASUREMENT_FLOOR" not in {d.code for d in stats.diagnostics()}


def test_every_reported_quantile_can_be_named_a_bound():
    """A window entirely under the floor bounds all four, not just the median."""
    accumulator = StatsAccumulator(0)
    words = array("I", [pack_sample(adc=0, range_index=0, counter=i, logic=0) for i in range(64)])
    accumulator.add_block(SampleBlock(0, words), [0.01] * 64)
    stats = accumulator.finalize()
    assert stats.quantiles_at_floor == ["p50", "p90", "p99", "p999"]
