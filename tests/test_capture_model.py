"""Timeline mapping, gap-aware iteration, and window statistics."""

import math

import pytest

from ppk2lab.capture.model import CaptureBuilder, CaptureMeta
from ppk2lab.capture.stats import compute_stats, latency_until_below
from ppk2lab.diagnostics import W_GAP_TABLE_TRUNCATED, W_WINDOW_UNPOPULATED
from ppk2lab.errors import CalibrationUnavailableError
from ppk2lab.protocol.samples import GapEvent, SampleBlock
from ppk2lab.testing.profiles import StepProfile

from .conftest import capture_of


def test_complete_capture_roundtrip_values():
    capture = capture_of(StepProfile([(100, 10.0, 0), (100, 5000.0, 1)]), samples=200)
    assert capture.complete
    currents = capture.currents_ua()
    assert currents[0] == pytest.approx(10.0, rel=0.01)
    assert currents[150] == pytest.approx(5000.0, rel=0.01)
    assert capture.logic_bytes()[:100] == b"\x00" * 100
    assert capture.logic_bytes()[100:200] == b"\x01" * 100


def test_gap_advances_timeline_without_shifting_data():
    capture = capture_of(StepProfile([(1000, 100.0, 0)], repeat=True), samples=300, gaps={100: 40})
    assert not capture.complete
    assert len(capture.gaps) == 1
    assert capture.gaps[0].index == 100
    assert capture.gaps[0].missing == 40
    # stored sample 100 sits at timeline index 140
    assert capture.stored_to_timeline(100) == 140
    assert capture.stored_to_timeline(99) == 99
    assert capture.timeline_to_stored(140) == 100
    assert capture.timeline_to_stored(120) is None  # inside the gap
    # 300 timeline positions were requested: 260 stored + 40 missing
    assert capture.stored_count == 260
    assert capture.end_index == 300


def test_iter_events_interleaves_gaps_in_timeline_order():
    capture = capture_of(
        StepProfile([(1000, 100.0, 0)], repeat=True), samples=300, gaps={100: 40, 200: 8}
    )
    events = list(capture.iter_events(block_samples=64))
    kinds = [type(e).__name__ for e in events]
    assert "GapEvent" in kinds
    position = 0
    for event in events:
        if isinstance(event, SampleBlock):
            assert event.start_index >= position
            position = event.end_index
        else:
            assert event.index == position
            position += event.missing or 0
    assert position == capture.end_index


def test_window_stats_clip_gaps():
    capture = capture_of(StepProfile([(1000, 100.0, 0)], repeat=True), samples=300, gaps={100: 40})
    stats = compute_stats(capture, start_index=50, end_index=200)
    assert stats.gap_count == 1
    assert stats.missing_samples_known == 40
    assert not stats.complete
    # 150 timeline positions minus 40 missing = 110 stored samples
    assert stats.stored_samples == 110
    clean = compute_stats(capture, start_index=0, end_index=100)
    assert clean.complete
    assert clean.stored_samples == 100


def test_charge_and_energy_integration():
    capture = capture_of(StepProfile([(1000, 1000.0, 0)]), samples=1000)
    stats = compute_stats(capture)
    # 1000 samples * 10us * 1000 uA = 10 uC
    assert stats.charge_uc == pytest.approx(10.0, rel=0.01)
    # VDD is 3.0 V in the simulator
    assert stats.energy_uj == pytest.approx(30.0, rel=0.01)
    assert stats.mean_ua == pytest.approx(1000.0, rel=0.01)
    assert stats.peak_index is not None


def test_latency_until_below():
    profile = StepProfile([(500, 5000.0, 0), (500, 8.0, 0)])
    capture = capture_of(profile, samples=1000)
    index = latency_until_below(capture, from_index=0, threshold_ua=100.0, hold_samples=10)
    assert index == 500


def test_latency_gap_resets_hold():
    profile = StepProfile([(100, 5000.0, 0), (900, 8.0, 0)])
    capture = capture_of(profile, samples=600, gaps={105: 20})
    index = latency_until_below(capture, from_index=0, threshold_ua=100.0, hold_samples=10)
    # the below-threshold run is interrupted by the gap at 105; the hold must
    # restart after it
    assert index is not None
    assert index >= 125


def test_summary_and_sha_stable():
    capture = capture_of(StepProfile([(100, 100.0, 0)]), samples=100)
    assert capture.sha256() == capture.sha256()
    summary = capture.summary()
    assert summary["samples"]["stored"] == 100
    assert summary["complete"]


# ---------------------------------------------------------------------------
# Window population
#
# "No gap event fell inside the window" and "the window is populated" are
# different claims. A window past the end of a capture satisfies the first and
# fails the second, and `complete` is the boolean a CI job reads to decide
# whether to trust the row.


def test_window_past_the_capture_end_is_not_complete():
    """A 10 s window over a 50 ms capture reported complete: true,
    charge_is_lower_bound: false and no warnings, on data that was 0.5%
    there."""
    capture = capture_of(StepProfile([(5000, 100.0, 0)]), samples=5000)
    stats = compute_stats(capture, start_index=0, end_index=1_000_000)
    assert stats.stored_samples == 5000
    assert stats.gap_count == 0  # nothing to find: the window is empty out there
    assert stats.unpopulated_samples == 995_000
    assert not stats.complete
    assert stats.charge_is_lower_bound
    assert stats.covered_fraction == pytest.approx(0.005)
    assert W_WINDOW_UNPOPULATED in {w.code for w in stats.diagnostics()}


def test_a_populated_gap_free_window_stays_complete():
    """The false-alarm guard: the ordinary case must not acquire a flag."""
    capture = capture_of(StepProfile([(1000, 100.0, 0)]), samples=1000)
    whole = compute_stats(capture)
    assert whole.complete
    assert whole.unpopulated_samples == 0
    assert not whole.charge_is_lower_bound
    assert whole.diagnostics() == []
    inner = compute_stats(capture, start_index=100, end_index=900)
    assert inner.complete
    assert inner.unpopulated_samples == 0


def test_a_window_that_starts_before_the_first_sample_is_unpopulated():
    """Triggered captures start at a nonzero timeline index; a window reaching
    back before it holds no data either."""
    capture = capture_of(StepProfile([(500, 100.0, 0)]), samples=500)
    capture.meta.start_index = 1_000
    stats = compute_stats(capture, start_index=0, end_index=1_500)
    assert stats.stored_samples == 500
    assert stats.unpopulated_samples == 1_000
    assert not stats.complete


def test_unknown_gaps_leave_population_unknowable_rather_than_guessed():
    capture = capture_of(StepProfile([(300, 100.0, 0)], repeat=True), samples=300)
    capture.gaps = [GapEvent(index=100, missing=None, reason="usb_stall", ambiguous=True)]
    stats = compute_stats(capture, start_index=0, end_index=300)
    assert stats.has_unknown_gaps
    assert stats.unpopulated_samples is None
    assert stats.covered_fraction is None
    assert not stats.complete  # the gap alone settles it


def test_gap_table_truncation_is_counted_and_degrades_the_timeline():
    """A desync storm produces gaps faster than any table can hold. The count
    stays exact, but positions past the ceiling cannot be resolved, so the
    timeline is degraded and the loss is reported rather than dropped."""
    builder = CaptureBuilder(CaptureMeta())
    builder.add(SampleBlock(0, __import__("array").array("I", [0] * 10)))
    for index in range(CaptureBuilder.MAX_GAPS + 1):
        builder.add(GapEvent(index=index, missing=1, reason="counter_skip", ambiguous=True))
    capture = builder.finish(complete=False)
    assert builder.gaps_truncated == 1
    assert capture.gaps_truncated == 1
    assert len(capture.gaps) == CaptureBuilder.MAX_GAPS
    assert capture.timeline_degraded


def test_truncated_gaps_surface_in_window_statistics():
    capture = capture_of(StepProfile([(300, 100.0, 0)]), samples=300)
    capture.gaps_truncated = 7
    stats = compute_stats(capture)
    assert stats.gaps_truncated == 7
    assert W_GAP_TABLE_TRUNCATED in {w.code for w in stats.diagnostics()}


def test_statistics_without_calibration_raise_a_typed_error():
    """`except Ppk2labError` is the documented contract, and "this capture
    cannot be converted" is not a bug in the caller's arguments."""
    capture = capture_of(StepProfile([(100, 100.0, 0)]), samples=100)
    capture.meta.metadata_text = None
    capture._calibration = None
    with pytest.raises(CalibrationUnavailableError):
        capture.currents_ua()
    with pytest.raises(CalibrationUnavailableError):
        compute_stats(capture)
    with pytest.raises(CalibrationUnavailableError):
        latency_until_below(capture, from_index=0, threshold_ua=1.0)


def test_currents_nan_preserved_for_missing_calibration():
    capture = capture_of(StepProfile([(100, 10.0, 0)]), samples=50)  # range 0
    # strip range-0 calibration from the stored metadata evidence
    capture.meta.metadata_text = capture.meta.metadata_text.replace("R0:", "RX:")
    capture._calibration = None  # force re-parse
    currents = capture.currents_ua()
    assert all(math.isnan(v) for v in currents)  # all samples are range 0
