"""Timeline mapping, gap-aware iteration, and window statistics."""

import math

import pytest

from ppk2lab.capture.stats import compute_stats, latency_until_below
from ppk2lab.protocol.samples import SampleBlock
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


def test_currents_nan_preserved_for_missing_calibration():
    capture = capture_of(StepProfile([(100, 10.0, 0)]), samples=50)  # range 0
    # strip range-0 calibration from the stored metadata evidence
    capture.meta.metadata_text = capture.meta.metadata_text.replace("R0:", "RX:")
    capture._calibration = None  # force re-parse
    currents = capture.currents_ua()
    assert all(math.isnan(v) for v in currents)  # all samples are range 0
