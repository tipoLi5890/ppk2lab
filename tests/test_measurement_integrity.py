"""Measurement-integrity guarantees derived from PPK2 hardware facts.

Each test pins a property that follows from what the hardware can and cannot
do — the 6-bit sample counter, the fixed 4-byte frame, the absence of any
voltage measurement — so a refactor cannot quietly trade truthfulness for
convenience.
"""

from __future__ import annotations

import json
import math
import struct
import threading
import time
import zipfile

import pytest

from ppk2lab.capture.runner import (
    ANCHOR_JITTER_S,
    DEFAULT_IN_MEMORY_LIMIT_SAMPLES,
    MIN_RATE_CHECK_SECONDS,
    PROGRESS_INTERVAL_S,
    timeline_report,
)
from ppk2lab.capture.stats import IMPLAUSIBLE_CURRENT_UA, VoltageContext, compute_stats
from ppk2lab.device import PPK2
from ppk2lab.diagnostics import (
    W_CLIPPED,
    W_DUT_POWER_UNKNOWN,
    W_NOT_CALIBRATED,
    W_TIMELINE_COMPRESSION,
    W_UNACCOUNTED_SAMPLES,
    W_VOLTAGE_ASSUMED,
    as_json,
)
from ppk2lab.discovery import _classify, _interface_number
from ppk2lab.errors import CaptureFileError, UsageError, VoltageRangeError
from ppk2lab.exports import export_csv
from ppk2lab.protocol.samples import (
    ADC_FULL_SCALE,
    COUNTER_MASK,
    DESYNC_CONSECUTIVE_MISMATCHES,
    SampleBlock,
    SampleStreamParser,
    pack_sample,
)
from ppk2lab.session import StreamSession
from ppk2lab.testing.profiles import ConstantProfile, DemoActivityProfile, StepProfile
from ppk2lab.transport.base import Transport
from ppk2lab.transport.mock import MockTransport, SimulatedPPK2
from ppk2lab.types import GapEvent, Mode, PortInfo, PortRole, VoltageBasis

from .conftest import capture_of, open_simulated


def _codes(warnings) -> set[str]:
    return {w["code"] for w in as_json(list(warnings))}


# ---------------------------------------------------------------------------
# Voltage provenance
#
# The PPK2 is a current meter: the sample stream carries an ADC code and a
# range, never a voltage at the DUT. Energy is therefore always charge times
# an assumed voltage, and which assumption applies depends on the mode.


def test_ampere_mode_refuses_to_invent_energy():
    """In Ampere mode the DUT runs from its own supply; the device's VDD
    field is a leftover setpoint, so energy must not be derived from it."""
    device = PPK2.open(
        transport=MockTransport(SimulatedPPK2(initial_mode=Mode.AMPERE)), simulate=True
    )
    try:
        result = device.capture(duration_s=0.05)
    finally:
        device.close()
    stats = result.stats.to_json()
    assert stats["energy_uj"] is None
    assert stats["charge_uc"] is not None  # charge needs no voltage at all
    assert stats["voltage_measured"] is False
    assert W_VOLTAGE_ASSUMED in _codes(result.warnings)


def test_ampere_mode_energy_with_an_explicit_assumption():
    device = PPK2.open(
        transport=MockTransport(SimulatedPPK2(initial_mode=Mode.AMPERE)), simulate=True
    )
    try:
        result = device.capture(duration_s=0.05, assume_voltage_mv=3300)
    finally:
        device.close()
    stats = result.stats.to_json()
    assert stats["energy_uj"] is not None
    assert stats["voltage_basis"] == VoltageBasis.CALLER_OVERRIDE.value
    assert stats["voltage_measured"] is False
    assert stats["energy_uj"] == pytest.approx(stats["charge_uc"] * 3.3, rel=1e-9)


def test_source_mode_energy_is_defensible_but_flagged_as_a_setpoint():
    device = PPK2.open(transport=MockTransport(SimulatedPPK2()), simulate=True)
    try:
        result = device.capture(duration_s=0.05)
    finally:
        device.close()
    stats = result.stats.to_json()
    assert stats["energy_uj"] is not None
    assert stats["voltage_basis"] == VoltageBasis.DEVICE_METADATA.value
    assert "setpoint" in stats["energy_note"]


def test_voltage_context_policy_matrix():
    caller = VoltageContext(3300, VoltageBasis.CALLER_OVERRIDE.value, "ampere")
    assert caller.energy_defensible  # an explicit assumption is always usable
    assert not VoltageContext(3000, VoltageBasis.DEVICE_METADATA.value, "ampere").energy_defensible
    assert VoltageContext(3000, VoltageBasis.DEVICE_METADATA.value, "source").energy_defensible
    assert not VoltageContext(None, VoltageBasis.UNKNOWN.value, "source").energy_defensible


def test_configuring_voltage_outranks_a_metadata_readback(sim_device):
    assert sim_device.state.source_voltage_basis is VoltageBasis.DEVICE_METADATA
    sim_device.set_source_voltage_mv(3300)
    assert sim_device.state.source_voltage_basis is VoltageBasis.CONFIGURED_SOURCE


# ---------------------------------------------------------------------------
# Timeline honesty
#
# The counter field is 6 bits wide (sample bits 18-23), so it can express at
# most 63 missing samples. Larger losses alias modulo 64, and a loss of
# exactly k*64 leaves the counter continuous — indistinguishable from no loss
# at all. The device cannot self-report large losses; only an independent
# time reference can.


def test_counter_aliases_losses_beyond_63_samples():
    """A tested limit rather than a comment: 100 missing samples are reported
    as 36, because 100 mod 64 is all a 6-bit counter can say."""
    capture = capture_of(StepProfile([(500, 100.0, 0)], repeat=True), samples=400, gaps={100: 100})
    assert [g.missing for g in capture.gaps] == [36]
    assert all(g.ambiguous for g in capture.gaps)


def test_loss_of_exactly_64_samples_is_invisible_to_the_counter():
    """The sharpest form of the aliasing problem: a multiple-of-64 loss
    leaves the counter perfectly continuous, so it produces no gap at all.
    Only the wall-clock cross-check can ever notice it."""
    capture = capture_of(StepProfile([(500, 100.0, 0)], repeat=True), samples=300, gaps={100: 64})
    assert capture.gaps == []
    assert capture.complete  # the sample stream alone cannot know better


def test_timeline_report_distinguishes_starvation_from_a_short_run():
    ok = timeline_report(
        first_sample_at=0.0,
        last_sample_at=10.0,
        timeline_advance=1_000_000,
        started_utc="",
        ended_utc="",
    )
    assert ok["rate_check"] == "ok"
    assert ok["achieved_sample_rate_hz"] == pytest.approx(100_000)

    starved = timeline_report(
        first_sample_at=0.0,
        last_sample_at=10.0,
        timeline_advance=320_000,  # 10 s of wall clock, only 3.2 s of samples
        started_utc="",
        ended_utc="",
    )
    assert starved["rate_check"] == "deficit"
    assert starved["rate_deficit_ratio"] == pytest.approx(0.68)

    brief = timeline_report(
        first_sample_at=0.0,
        last_sample_at=MIN_RATE_CHECK_SECONDS / 2,
        timeline_advance=1,
        started_utc="",
        ended_utc="",
    )
    assert brief["rate_check"] == "too_short"  # jitter dominates; do not guess


def test_timeline_report_estimates_loss_the_counter_cannot_describe():
    """A loss of exactly k*64 samples leaves the counter continuous, so the
    only witness is that wall time accounts for samples the timeline does
    not. The estimate is the difference; the floor is what host anchoring and
    two unsynchronized clocks can produce on their own."""
    report = timeline_report(
        first_sample_at=0.0,
        last_sample_at=10.0,
        timeline_advance=950_000,  # 10 s of wall clock, 9.5 s of timeline
        started_utc="",
        ended_utc="",
        first_block_samples=4096,
        last_block_samples=4096,
    )
    assert report["unaccounted_samples_estimate"] == 50_000
    # anchoring (two blocks + two poll intervals) plus 500 ppm of clock slack
    assert report["unaccounted_floor_samples"] == (
        4096 + 4096 + round(2 * ANCHOR_JITTER_S * 100_000) + round(5e-4 * 1_000_000)
    )
    assert report["unaccounted_samples_estimate"] > report["unaccounted_floor_samples"]
    # rate_deficit_ratio stays clamped; its signed companion does not
    assert report["rate_deficit_ratio"] == pytest.approx(0.05)
    assert report["rate_offset_ratio"] == pytest.approx(-0.05)


def test_a_timeline_that_outruns_wall_time_is_reported_signed_not_clamped():
    """Both anchors are stamped on delivery, so a short capture can look
    faster than 100 kS/s. Clamping that to a zero deficit hides an artifact
    the reader needs in order to judge the estimate."""
    report = timeline_report(
        first_sample_at=0.0,
        last_sample_at=1.0,
        timeline_advance=104_000,
        started_utc="",
        ended_utc="",
        first_block_samples=4096,
        last_block_samples=4096,
    )
    assert report["rate_deficit_ratio"] == 0.0
    assert report["rate_offset_ratio"] == pytest.approx(0.04)
    assert report["unaccounted_samples_estimate"] == -4_000


def test_triggered_capture_emits_no_unaccounted_estimate():
    """Wall time covers only the post-trigger window while the timeline covers
    the pre-trigger ring buffer too. An estimate from those two intervals
    would flag every triggered capture as lossy."""
    report = timeline_report(
        first_sample_at=0.0,
        last_sample_at=10.0,
        timeline_advance=950_000,
        started_utc="",
        ended_utc="",
        applicable=False,
    )
    assert report["unaccounted_samples_estimate"] is None
    assert report["unaccounted_floor_samples"] is None
    assert report["rate_check"] == "not_applicable"


@pytest.mark.slow
def test_sub_threshold_wall_clock_loss_still_marks_charge_a_lower_bound():
    """A 7% shortfall stays under the compression threshold and reports
    rate_check "ok" — but the samples are gone all the same, and the integral
    that omits them is a lower bound."""
    device = PPK2.open(transport=MockTransport(SimulatedPPK2(rate_limit_hz=93_000)), simulate=True)
    try:
        result = device.capture(duration_s=4.0, in_memory_limit_samples=None)
    finally:
        device.close()
    timeline = result.timeline
    assert timeline["rate_check"] == "ok"  # below RATE_DEFICIT_TOLERANCE
    assert timeline["unaccounted_samples_estimate"] > timeline["unaccounted_floor_samples"]
    assert result.stats.charge_is_lower_bound
    assert result.stats.unaccounted_loss_is_capture_level
    assert W_UNACCOUNTED_SAMPLES in _codes(result.warnings)
    # Capture-level, so nothing pretends to place it inside the window.
    assert result.stats.covered_fraction == 1.0


@pytest.mark.slow
def test_full_rate_capture_gains_no_lower_bound_flag():
    """The false-alarm guard for the estimate: a healthy stream must not
    acquire a lower-bound flag out of host jitter."""
    device = PPK2.open(transport=MockTransport(SimulatedPPK2(rate_limit_hz=100_000)), simulate=True)
    try:
        result = device.capture(duration_s=2.5, in_memory_limit_samples=None)
    finally:
        device.close()
    assert not result.stats.unaccounted_loss_is_capture_level
    assert not result.stats.charge_is_lower_bound
    assert W_UNACCOUNTED_SAMPLES not in _codes(result.warnings)


def test_stored_wall_clock_deficit_reaches_offline_statistics():
    """The witness is written into the artifact, so a capture read back days
    later must still report its integrals as lower bounds."""
    from ppk2lab.capture.model import Capture, CaptureMeta

    base = capture_of(StepProfile([(300, 100.0, 0)]), samples=300)
    lossy = Capture(
        CaptureMeta(
            device=base.meta.device,
            configuration=base.meta.configuration,
            metadata_text=base.meta.metadata_text,
        ),
        base.words,
        [],
        timing={"unaccounted_samples_estimate": 50_000, "unaccounted_floor_samples": 12_000},
    )
    stats = compute_stats(lossy)
    assert stats.unaccounted_samples_estimate == 50_000
    assert stats.unaccounted_loss_is_capture_level
    assert stats.charge_is_lower_bound
    assert W_UNACCOUNTED_SAMPLES in {w.code for w in stats.diagnostics()}
    # Inside the floor the same figure is arithmetic on two clocks, not loss.
    quiet = Capture(
        lossy.meta,
        base.words,
        [],
        timing={"unaccounted_samples_estimate": 500, "unaccounted_floor_samples": 12_000},
    )
    assert not compute_stats(quiet).charge_is_lower_bound


@pytest.mark.slow
def test_starved_stream_is_reported_incomplete():
    device = PPK2.open(transport=MockTransport(SimulatedPPK2(rate_limit_hz=70_000)), simulate=True)
    try:
        result = device.capture(duration_s=2.5)
    finally:
        device.close()
    assert result.timeline["rate_check"] == "deficit"
    assert not result.complete
    assert result.interruption["reason"] == "timeline_compression"
    assert W_TIMELINE_COMPRESSION in _codes(result.warnings)


@pytest.mark.slow
def test_full_rate_stream_is_not_falsely_flagged():
    """The check must not cry wolf on a healthy capture."""
    device = PPK2.open(transport=MockTransport(SimulatedPPK2(rate_limit_hz=100_000)), simulate=True)
    try:
        result = device.capture(duration_s=2.5)
    finally:
        device.close()
    assert result.timeline["rate_check"] == "ok"
    assert result.complete


# ---------------------------------------------------------------------------
# Stream stalls
#
# A USB CDC port that stops delivering data raises nothing: the read simply
# returns empty. Silence is indistinguishable from a stopped device unless
# the host imposes its own budget.


def test_silent_device_ends_the_capture_instead_of_hanging():
    simulator = SimulatedPPK2()
    original_read = simulator.read

    def mute(max_bytes, timeout_s=0.1):
        original_read(max_bytes, timeout_s)  # keep command handling alive
        return b""

    device = PPK2.open(transport=MockTransport(simulator), simulate=True)
    simulator.read = mute
    try:
        from ppk2lab.session import DEFAULT_IDLE_TIMEOUT_S

        assert DEFAULT_IDLE_TIMEOUT_S <= 10, "idle budget must stay bounded"
        result = device.capture(duration_s=0.05, in_memory_limit_samples=None)
    finally:
        simulator.read = original_read
        device.close()
    assert result.interruption is not None
    assert result.interruption["reason"] == "stream_stalled"
    assert not result.complete


# ---------------------------------------------------------------------------
# Calibration provenance
#
# A missing constant must never become a default: O[r] = 0 alone biases every
# sample in that range by the full offset. Unknown stays unknown, and the
# capture records why.


def test_calibration_state_reaches_the_result_and_the_artifact(tmp_path):
    simulator = SimulatedPPK2()
    simulator.metadata_text = lambda: SimulatedPPK2.metadata_text(simulator).replace(
        "Calibrated: 1", "Calibrated: 0"
    )
    device = PPK2.open(transport=MockTransport(simulator), simulate=True)
    path = tmp_path / "cal.ppk2a"
    try:
        result = device.capture(duration_s=0.05, output=str(path))
    finally:
        device.close()
    assert W_NOT_CALIBRATED in _codes(result.warnings)
    with zipfile.ZipFile(path) as zf:
        manifest = json.loads(zf.read("manifest.json"))
    assert manifest["calibration"]["calibrated_flag"] is False
    assert manifest["calibration"]["missing_ranges"] == []
    assert any(w["code"] == W_NOT_CALIBRATED for w in manifest["warnings"])


def test_dut_power_unknown_is_advised_not_hidden():
    device = PPK2.open(transport=MockTransport(SimulatedPPK2()), simulate=True)
    try:
        result = device.capture(duration_s=0.05)
    finally:
        device.close()
    assert W_DUT_POWER_UNKNOWN in _codes(result.warnings)


# ---------------------------------------------------------------------------
# Byte-level framing loss
#
# Samples are fixed 4-byte frames with no sync word. Losing a byte run that
# is not a multiple of four shifts every later frame boundary, so the counter
# field reads bits belonging to its neighbours and nearly every frame looks
# like a counter jump.


def _raw_stream(n: int, adc: int = 100, range_index: int = 1) -> bytes:
    return b"".join(struct.pack("<I", pack_sample(adc, range_index, i & 0x3F, 0)) for i in range(n))


@pytest.mark.parametrize("lost_bytes", [1, 2, 3])
def test_partial_byte_loss_does_not_fabricate_timeline(lost_bytes):
    """A lost byte must not become invented elapsed time.

    The counter field of a shifted word reads bits belonging to its
    neighbours, so treating each mismatch as a device-side skip advances the
    timeline by a random amount per word. Losing one byte this way inflated a
    2000-sample stream to next_index 5713 — 3715 phantom samples, 37 ms of
    time that never happened — which is the very failure the timeline is
    supposed to make impossible.
    """
    parser = SampleStreamParser()
    data = _raw_stream(2000)
    corrupted = data[:4000] + data[4000 + lost_bytes :]
    events = []
    for offset in range(0, len(corrupted), 137):  # arbitrary chunking
        events.extend(parser.feed(corrupted[offset : offset + 137]))
    gaps = [e for e in events if isinstance(e, GapEvent)]

    assert parser.desync_events == 1
    assert [g.reason for g in gaps] == ["stream_desync"]  # no counter_skip at all
    assert sum(g.missing or 0 for g in gaps) == 0  # nothing fabricated
    assert parser.timeline_degraded  # the loss is still declared
    assert parser.next_index == 1995  # honest slight under-count, not 5713
    assert parser.samples_emitted == 1995  # realignment recovered the stream


@pytest.mark.parametrize("chunking", [4, 8, 16, 137, 512, 8192])
def test_desync_timeline_inflation_is_bounded_for_any_chunking(chunking):
    """State the guarantee rather than relying on a lucky chunk size.

    At most one unconfirmed gap can be flushed per feed() call, and the
    consecutive-mismatch counter is parser-wide, so the verdict caps the leak
    regardless of how the bytes arrive.
    """
    parser = SampleStreamParser()
    data = _raw_stream(2000)
    corrupted = data[:4000] + data[4001:]
    events = []
    for offset in range(0, len(corrupted), chunking):
        events.extend(parser.feed(corrupted[offset : offset + chunking]))
    skips = [e for e in events if isinstance(e, GapEvent) and e.reason == "counter_skip"]
    assert len(skips) <= DESYNC_CONSECUTIVE_MISMATCHES - 1
    assert sum(g.missing for g in skips) <= (DESYNC_CONSECUTIVE_MISMATCHES - 1) * COUNTER_MASK
    assert parser.desync_events == 1
    assert parser.samples_emitted > 1900


def test_unrecoverable_bytes_do_not_cause_a_desync_storm():
    """Bytes with no recoverable framing must be dropped once, not re-entered.

    Adopting the best-scoring offset unconditionally re-desyncs a few words
    later and loops; a minimum score turns that into a single event.
    """
    import random

    parser = SampleStreamParser()
    rng = random.Random(7)
    counters = list(range(100)) + [rng.randrange(64) for _ in range(1900)]
    data = b"".join(struct.pack("<I", pack_sample(100, 1, c & 0x3F, 0)) for c in counters)
    parser.feed(data)
    assert parser.desync_events == 1
    assert parser.next_index == 100  # only the clean prefix reached the timeline
    assert parser.flush_stats()["missing_samples_known"] == 0


def test_real_gaps_do_not_look_like_a_desync():
    parser = SampleStreamParser()
    counters = [i + 5 * (i // 500) for i in range(3000)]  # six genuine skips
    data = b"".join(struct.pack("<I", pack_sample(100, 1, c & 0x3F, 0)) for c in counters)
    parser.feed(data)
    assert parser.desync_events == 0
    assert parser.flush_stats()["gaps"] == 5


def test_implausible_currents_are_excluded_and_counted():
    capture = capture_of(StepProfile([(200, 100.0, 0)]), samples=200)
    stats = compute_stats(capture)
    assert stats.implausible_samples == 0
    assert IMPLAUSIBLE_CURRENT_UA > 1_000_000  # above the top range's 1 A


# ---------------------------------------------------------------------------
# The instrument's own ceiling
#
# The ADC field is 14 bits. A DUT beyond the top of the selected range pins it
# at full scale, and the calibrated value that comes back is by construction
# just under the range maximum — plausible, flat, and wrong. Nothing about the
# converted number can reveal that; only the raw code can.


def test_saturated_samples_are_detected_and_force_a_lower_bound():
    """A 2.5 A load reported mean and max of exactly 1000000.0 uA with
    complete: true — a 2.5x error rendered as a perfectly regulated flat top.
    The plausibility ceiling cannot catch it: a pinned sample sits below 1 A."""
    device = PPK2.open(
        transport=MockTransport(SimulatedPPK2(profile=ConstantProfile(2_500_000.0))), simulate=True
    )
    try:
        result = device.capture(duration_s=0.05)
    finally:
        device.close()
    stats = result.stats
    assert stats.implausible_samples == 0  # the old guard still sees nothing
    assert stats.saturated_samples == stats.stored_samples
    assert stats.saturated_ranges == [4]  # the top range: past the 1 A span
    assert stats.charge_is_lower_bound
    assert W_CLIPPED in _codes(result.warnings)


def test_an_ordinary_load_is_not_reported_as_clipped():
    """The false-alarm guard: a 100 uA load sits mid-range and must acquire
    neither a saturation count nor a lower-bound flag."""
    device = open_simulated()
    try:
        result = device.capture(duration_s=0.05)
    finally:
        device.close()
    assert result.stats.saturated_samples == 0
    assert result.stats.saturated_ranges == []
    assert not result.stats.charge_is_lower_bound
    assert W_CLIPPED not in _codes(result.warnings)


def test_range_occupancy_partitions_the_valid_samples():
    """Per-range counts and charges are a decomposition, not a second opinion:
    they must add up to the totals computed alongside them."""
    device = PPK2.open(
        transport=MockTransport(SimulatedPPK2(profile=DemoActivityProfile())), simulate=True
    )
    try:
        result = device.capture(sample_limit=300_000, in_memory_limit_samples=None)
    finally:
        device.close()
    stats = result.stats
    assert sum(stats.samples_per_range) == stats.valid_samples
    assert sum(stats.charge_per_range_uc) == pytest.approx(stats.charge_uc, rel=1e-12)
    # 3 s of the demo cycle: 6 uA sleep in range 0, 12 mA bursts in range 3.
    assert stats.samples_per_range == (255_000, 0, 0, 45_000, 0)
    assert stats.range_switches == 59
    assert stats.range_switch_rate_hz == pytest.approx(59 / 3.0)


def test_a_sample_that_cannot_be_converted_lands_in_no_range():
    """Range occupancy is computed over the samples that reached the mean; a
    sample excluded from the statistics must not appear in the decomposition
    either, or the two stop agreeing."""
    capture = capture_of(StepProfile([(100, 10.0, 0)]), samples=100)  # all range 0
    capture.meta.metadata_text = capture.meta.metadata_text.replace("R0:", "RX:")
    capture._calibration = None  # force a re-parse without range 0
    stats = compute_stats(capture)
    assert stats.nan_samples == 100
    assert stats.valid_samples == 0
    assert stats.samples_per_range == (0, 0, 0, 0, 0)
    assert stats.charge_per_range_uc is None


def test_range_switches_are_not_counted_across_a_gap():
    """Two samples separated by missing data are not adjacent, so a range
    difference between them is not an observed switch."""
    capture = capture_of(
        StepProfile([(100, 10.0, 0), (100, 5000.0, 0)]), samples=200, gaps={100: 40}
    )
    stats = compute_stats(capture)
    assert stats.gap_count == 1
    # The only range change in this profile sits exactly at the gap.
    assert stats.range_switches == 0


def test_full_scale_is_the_top_code_of_the_fourteen_bit_field():
    assert ADC_FULL_SCALE == 0x3FFF
    assert pack_sample(ADC_FULL_SCALE, 4, 0, 0) & 0x3FFF == ADC_FULL_SCALE


# ---------------------------------------------------------------------------
# Memory, metadata delivery, and safety ceilings


def test_capture_without_a_stop_condition_is_a_typed_usage_error():
    """`except Ppk2labError` is the documented contract; a bare ValueError
    from the most obvious public entry point escapes it."""
    from ppk2lab.errors import Ppk2labError

    device = open_simulated()
    try:
        with pytest.raises(UsageError) as excinfo:
            device.capture()
        assert isinstance(excinfo.value, Ppk2labError)
        assert excinfo.value.exit_code == 2
        assert "duration" in excinfo.value.remediation
    finally:
        device.close()


def test_unbounded_in_memory_capture_is_refused():
    device = open_simulated()
    try:
        with pytest.raises(UsageError) as excinfo:
            device.capture(duration_s=DEFAULT_IN_MEMORY_LIMIT_SAMPLES / 100_000 + 1)
        assert "--output" in excinfo.value.remediation
    finally:
        device.close()


def test_metadata_split_across_reads_keeps_every_constant():
    """The reply is newline-delimited text of about a kilobyte, so a serial
    stack is free to deliver it in several reads. Parsing before the
    terminator arrives would silently drop whichever constants came last."""
    simulator = SimulatedPPK2(metadata_chunk_bytes=17)  # forces many splits
    device = PPK2.open(transport=MockTransport(simulator), simulate=True)
    try:
        metadata = device.metadata
        assert metadata is not None
        assert metadata.terminated
        assert metadata.missing_cal_ranges() == []
        assert device.calibration is not None
        assert device.calibration.missing_ranges() == []
    finally:
        device.close()


def test_stray_end_bytes_do_not_truncate_metadata():
    from ppk2lab.device import _metadata_terminated

    assert not _metadata_terminated(b"R0: 1000\nBEND: 3\n")
    assert _metadata_terminated(b"R0: 1000\nEND")
    assert _metadata_terminated(b"END")


def test_voltage_ceiling_refuses_before_touching_the_wire():
    simulator = SimulatedPPK2()
    device = PPK2.open(transport=MockTransport(simulator), simulate=True, max_voltage_mv=3600)
    try:
        log_before = list(simulator.command_log)
        with pytest.raises(VoltageRangeError):
            device.set_source_voltage_mv(5000)
        assert simulator.command_log == log_before  # nothing was sent
        device.set_source_voltage_mv(3300)  # below the ceiling still works
    finally:
        device.close()


def test_second_stream_on_one_device_is_refused(sim_device):
    stream = sim_device.stream(sample_limit=100_000)
    next(stream)  # activate
    try:
        with pytest.raises(UsageError):
            sim_device.set_mode(Mode.AMPERE)
        with pytest.raises(UsageError):
            next(sim_device.stream(sample_limit=10))
    finally:
        stream.close()


def test_state_changes_are_refused_while_measuring(sim_device):
    sim_device.start_measuring()
    try:
        with pytest.raises(UsageError):
            sim_device.set_source_voltage_mv(3300)
        with pytest.raises(UsageError):
            sim_device.reset()
    finally:
        sim_device.stop_measuring()


# ---------------------------------------------------------------------------
# Window statistics and exports


def test_unknown_gap_at_the_window_start_is_not_swallowed():
    """A zero-width unknown gap sitting exactly on the window boundary used
    to vanish, letting the window claim it was complete."""
    from ppk2lab.capture.model import Capture, CaptureMeta

    base = capture_of(StepProfile([(300, 100.0, 0)]), samples=300)
    gapped = Capture(
        CaptureMeta(
            device=base.meta.device,
            configuration=base.meta.configuration,
            metadata_text=base.meta.metadata_text,
        ),
        base.words,
        [GapEvent(index=100, missing=None, reason="usb_stall", ambiguous=True)],
        complete=False,
    )
    stats = compute_stats(gapped, start_index=100, end_index=200)
    assert stats.gap_count == 1
    assert stats.has_unknown_gaps
    assert not stats.complete


def test_coverage_and_lower_bound_are_reported():
    capture = capture_of(StepProfile([(400, 100.0, 0)], repeat=True), samples=400, gaps={100: 40})
    stats = compute_stats(capture)
    assert stats.charge_is_lower_bound
    assert stats.covered_fraction is not None
    assert stats.covered_fraction < 1.0


def test_export_refuses_to_report_a_partial_write(tmp_path, monkeypatch):
    capture = capture_of(StepProfile([(100, 100.0, 0)]), samples=100)
    path = tmp_path / "short.csv"
    # Simulate a capture whose stored count disagrees with what was written.
    monkeypatch.setattr(type(capture), "stored_count", property(lambda self: 999))
    with pytest.raises(CaptureFileError):
        export_csv(capture, path)


def test_manifest_without_a_sample_rate_is_refused(tmp_path):
    capture = capture_of(StepProfile([(100, 100.0, 0)]), samples=100)
    path = tmp_path / "cap.ppk2a"
    capture.save(str(path))
    with zipfile.ZipFile(path) as zf:
        items = {name: zf.read(name) for name in zf.namelist()}
    manifest = json.loads(items["manifest.json"])
    del manifest["timeline"]["sample_rate_hz"]
    items["manifest.json"] = json.dumps(manifest).encode()
    with zipfile.ZipFile(path, "w") as zf:
        for name, blob in items.items():
            zf.writestr(zipfile.ZipInfo(name), blob)
    with pytest.raises(CaptureFileError) as excinfo:
        capture.load(str(path))
    assert "sample_rate_hz" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Port classification
#
# Firmware 1.2.0 and newer expose a second CDC interface, and the OS reports
# the interface number differently per platform (or not at all).


class _Entry:
    def __init__(self, location):
        self.location = location


@pytest.mark.parametrize(
    "location,expected",
    [
        ("1-4:x.0", 0),  # Windows: non-numeric configuration field
        ("1-4:x.2", 2),
        ("1-1.1:1.0", 0),  # Linux
        ("20-3:1.2", 2),
        ("1-1.1", None),  # macOS: no interface information at all
    ],
)
def test_interface_number_parsing_across_platforms(location, expected):
    assert _interface_number(_Entry(location)) == expected


def test_measurement_port_is_the_lowest_interface():
    ports = [
        PortInfo(path="/dev/a", interface_number=2),
        PortInfo(path="/dev/b", interface_number=0),
    ]
    roles = {p.path: p.role for p in _classify(ports)}
    assert roles["/dev/b"] is PortRole.MEASUREMENT
    assert roles["/dev/a"] is PortRole.SHELL


def test_unclassifiable_ports_stay_unknown_rather_than_guessing():
    ports = [PortInfo(path="/dev/a"), PortInfo(path="/dev/b")]
    assert all(p.role is PortRole.UNKNOWN for p in _classify(ports))


# ---------------------------------------------------------------------------
# Machine-readable warnings


def test_warnings_carry_codes_agents_can_branch_on():
    device = PPK2.open(transport=MockTransport(SimulatedPPK2()), simulate=True)
    try:
        result = device.capture(duration_s=0.05)
    finally:
        device.close()
    payload = result.to_json()["warnings"]
    assert payload, "a capture with unknown DUT power should warn"
    for entry in payload:
        assert set(entry) == {"code", "message", "category"}
        assert entry["code"].startswith("W_")
        # The category travels with the warning so a consumer can route it
        # without fetching the capabilities catalog to join on the code.
        assert entry["category"] in {
            "capture integrity",
            "measurement trust",
            "device state",
            "analysis",
        }


def test_as_json_round_trips_stored_warnings():
    """A stored warning comes back in the same shape a live one has.

    This used to assert `as_json(stored) == stored`, which pinned the opposite:
    the dict branch rebuilt only {code, message}, so `category` was stripped on
    the way into a manifest and `capture --json` and `inspect --json` reported
    different shapes for the same warning.
    """
    stored = [{"code": "W_SAMPLE_GAPS", "message": "text"}]
    restored = as_json(stored)
    assert restored == [
        {"code": "W_SAMPLE_GAPS", "message": "text", "category": "capture integrity"}
    ]
    # Idempotent: normalizing an already-normalized list must not change it,
    # because a capture can be read, re-warned and rewritten.
    assert as_json(restored) == restored
    # A category already on the dict is preserved rather than re-derived.
    tagged = [{"code": "W_SAMPLE_GAPS", "message": "text", "category": "something else"}]
    assert as_json(tagged)[0]["category"] == "something else"
    # An unrecognised code yields None, matching Diagnostic.to_json's contract.
    assert as_json([{"code": "W_FROM_THE_FUTURE", "message": "x"}])[0]["category"] is None
    assert as_json(["bare string"])[0]["code"] == "W_GENERIC"


def test_sample_block_helpers_still_agree_with_raw_words():
    block = SampleBlock(0, __import__("array").array("I", [pack_sample(7, 2, 5, 0xA5)]))
    assert block.adc == [7]
    assert block.ranges == bytes([2])
    assert block.counters == bytes([5])
    assert block.logic == bytes([0xA5])


# ---------------------------------------------------------------------------
# Regressions found by auditing the integrity work itself


def test_older_artifact_keeps_its_energy(tmp_path):
    """Provenance was added later; a stored source-mode capture predates it.

    Voiding those energy figures would be a silent regression, so the mode
    recorded in the artifact stands in for the missing basis.
    """
    capture = capture_of(StepProfile([(500, 1000.0, 0)]), samples=500)
    capture.meta.configuration.pop("voltage_basis", None)
    capture.meta.configuration.pop("voltage_measured", None)
    assert capture.meta.configuration["mode"] == "source"
    stats = compute_stats(capture)
    assert stats.energy_uj is not None
    assert stats.voltage_basis == VoltageBasis.DEVICE_METADATA.value


def test_uncalibrated_capture_is_not_reported_as_starved():
    """The timeline is a property of the stream, not of the conversion.

    Reading the extent from the statistics accumulator meant a device opened
    without metadata advanced by zero and every capture looked 100% starved.
    """
    device = PPK2.open(transport=MockTransport(SimulatedPPK2()), simulate=True, read_metadata=False)
    try:
        result = device.capture(duration_s=0.05)
    finally:
        device.close()
    assert result.timeline["timeline_advance"] >= 5000
    assert result.timeline["rate_check"] != "deficit"
    assert result.complete


def test_triggered_capture_declines_the_rate_check():
    """Pre-trigger samples are emitted at the fire moment.

    Wall time then covers only the post-trigger window while the timeline
    covers both, so the ratio compares two different intervals — it could
    read 200 kS/s, or hide a 50% loss as healthy.
    """
    from ppk2lab.triggers.engine import DigitalEdgeTrigger, TriggerEngine

    device = PPK2.open(transport=MockTransport(SimulatedPPK2()), simulate=True)
    try:
        engine = TriggerEngine(
            DigitalEdgeTrigger(1, rising=True), pre_samples=1000, post_samples=2000
        )
        result = device.capture(trigger_engine=engine, trigger_timeout_s=5)
    finally:
        device.close()
    assert result.timeline["rate_check"] == "not_applicable"
    assert result.timeline["achieved_sample_rate_hz"] is None
    # And nothing derived from that comparison reaches the statistics, or
    # every triggered capture would carry a bogus lower-bound flag.
    assert result.timeline["unaccounted_samples_estimate"] is None
    assert not result.stats.unaccounted_loss_is_capture_level
    assert W_UNACCOUNTED_SAMPLES not in _codes(result.warnings)


def test_failed_stream_start_does_not_brick_the_handle():
    """Claiming the device before start meant a transient write error left
    the claim set forever, and every later command blamed a stream that had
    never begun."""
    simulator = SimulatedPPK2()
    device = PPK2.open(transport=MockTransport(simulator), simulate=True)
    original_write = device.transport.write

    def failing_write(data):
        if data == b"\x06":  # start measuring
            raise RuntimeError("simulated USB write failure")
        return original_write(data)

    device.transport.write = failing_write
    try:
        with pytest.raises(RuntimeError):
            list(device.stream(sample_limit=10))
        device.transport.write = original_write
        device.refresh_metadata()  # must not raise "a stream is active"
        assert device.capture(duration_s=0.01).stats is not None
    finally:
        device.transport.write = original_write
        device.close()


def test_in_memory_guard_cannot_be_bypassed_with_an_output_path():
    """The RAM cost comes from keeping samples, not from the absence of a file."""
    device = open_simulated()
    try:
        with pytest.raises(UsageError):
            device.capture(duration_s=120, output="unused.ppk2a", keep_in_memory=True)
    finally:
        device.close()


def test_desync_is_reported_as_a_coded_warning():
    """A warning code nothing emits is a contract that does not exist."""
    from ppk2lab.diagnostics import W_STREAM_DESYNC

    device = open_simulated()
    try:
        result = device.capture(duration_s=0.05)
        # No desync in a healthy capture...
        assert W_STREAM_DESYNC not in _codes(result.warnings)
        # ...but the parser's count is what drives the warning.
        assert device.last_session is not None
        assert hasattr(device.last_session.parser, "desync_events")
    finally:
        device.close()


def test_consecutive_captures_do_not_inherit_stale_bytes():
    """Bytes in flight when stop lands would otherwise be parsed as the head
    of the next capture on the same handle, shifting its framing."""
    device = open_simulated()
    try:
        first = device.capture(duration_s=0.02)
        second = device.capture(duration_s=0.02)
        assert first.stats is not None and second.stats is not None
        assert second.stats.stored_samples >= 1900
        assert second.timeline["timeline_advance"] >= 1900
        assert device.last_session is not None
        assert device.last_session.parser.desync_events == 0
    finally:
        device.close()


# -- the reader queue is bounded in bytes -------------------------------
#
# SerialTransport.read returns ``1 + in_waiting`` bytes, and in_waiting is
# small exactly when the reader is keeping up: a 30 s hardware capture
# averaged 77 bytes per read. Bounding the queue by item count therefore
# sized the buffer by how well the reader was doing — 256 items of 77 bytes
# is 20 kB, 50 ms of stream — so any consumer pause longer than that dropped
# samples. Bounding it in bytes is what makes the buffer mean what the
# constants say it means.


class _DribbleTransport(Transport):
    """A transport that answers in the small reads a real serial port does.

    Paces bytes at the PPK2's own 400 kB/s so a pause buffers a realistic
    amount rather than an unbounded one.
    """

    RATE_BYTES_S = 400_000
    READ_BYTES = 77

    def __init__(self) -> None:
        self._open = True
        self._start = time.monotonic()
        self._served = 0

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    def write(self, data: bytes) -> None:
        return None

    def read(self, max_bytes: int, timeout_s: float = 0.1) -> bytes:
        due = int((time.monotonic() - self._start) * self.RATE_BYTES_S) - self._served
        if due < self.READ_BYTES:
            time.sleep(min(timeout_s, 0.005))
            return b""
        n = min(self.READ_BYTES, max_bytes)
        self._served += n
        return bytes(n)

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def description(self) -> str:
        return "dribble://test"


def _pause_and_drain(queue_bytes: float) -> StreamSession:
    session = StreamSession(_DribbleTransport(), queue_bytes=int(queue_bytes))
    session.start()
    try:
        time.sleep(0.3)  # the consumer is busy; nothing is drained
    finally:
        session.stop()
    return session


@pytest.mark.slow
def test_a_consumer_pause_fits_in_the_byte_budget():
    """0.3 s of stream is 120 kB — comfortably inside the 4 MB buffer."""
    session = _pause_and_drain(4 * 1024 * 1024)
    assert session.dropped_bytes_total == 0
    assert 0 < session.peak_queued_bytes <= 4 * 1024 * 1024


@pytest.mark.slow
def test_the_same_pause_overflows_the_buffer_the_item_bound_really_gave():
    """256 items x 77 bytes was the effective size before; it drops here."""
    session = _pause_and_drain(256 * _DribbleTransport.READ_BYTES)
    assert session.dropped_bytes_total > 0


def test_the_queue_budget_counts_bytes_and_is_returned_on_consumption():
    session = StreamSession(_DribbleTransport(), queue_bytes=1000)
    assert session._reserve(600) is True
    assert session._reserve(600) is False, "600 + 600 must not fit in 1000 bytes"
    session._release(600)
    assert session._reserve(600) is True
    assert session.peak_queued_bytes == 600


class _FramedFloodTransport(Transport):
    """Serves a correctly framed sample stream in fixed-size reads.

    ``_DribbleTransport`` only needs its byte *counts* to be realistic; these
    bytes have to parse. A host-side drop reaches the timeline as a gap only
    if what surrounds it frames, and the 6-bit counter has to run unbroken
    across the drop so the gap under test is the one the host caused rather
    than a device-side skip. 1024 samples is a whole number of counter cycles,
    so one chunk served over and over keeps the counter continuous.
    """

    CHUNK_SAMPLES = 1024
    CHUNK_BYTES = CHUNK_SAMPLES * 4

    def __init__(self, *, burst_chunks: int | None = None) -> None:
        self._open = True
        self._chunk = _raw_stream(self.CHUNK_SAMPLES)
        self._remaining = burst_chunks
        self._extra = 0
        #: Set once the burst is spent, so a test can wait for the reader to
        #: have made every keep-or-drop decision it is going to make.
        self.burst_served = threading.Event()

    def resume(self, chunks: int = 1) -> None:
        """Let the reader have ``chunks`` further reads after the burst."""
        self._extra += chunks

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    def write(self, data: bytes) -> None:
        return None

    def read(self, max_bytes: int, timeout_s: float = 0.1) -> bytes:
        if self._remaining is not None and self._remaining <= 0:
            if self._extra <= 0:
                # Set here and not when the last chunk is handed over: the
                # reader only asks again once it has queued or dropped the
                # previous chunk, so a test waiting on this sees a settled
                # drop count.
                self.burst_served.set()
                time.sleep(min(timeout_s, 0.005))
                return b""
            self._extra -= 1
        elif self._remaining is not None:
            self._remaining -= 1
        time.sleep(0.001)  # a paced reader, not a spin loop pegging a core
        return self._chunk[:max_bytes] if max_bytes < len(self._chunk) else self._chunk

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def description(self) -> str:
        return "framed://test"


def test_a_host_side_overflow_reaches_the_timeline_as_a_gap():
    """Bytes the host threw away have to be priced into the timeline.

    The reader counts them, but the count only becomes measurement truth once
    the parser turns it into a ``GapEvent`` the consumer sees. Without the
    pending-drop hand-off the stream reads as continuous, and the samples that
    never arrived are absorbed into the indices on either side of the hole —
    loss reported nowhere the caller looks.
    """
    chunk = _FramedFloodTransport.CHUNK_BYTES
    transport = _FramedFloodTransport(burst_chunks=20)
    session = StreamSession(transport, read_chunk=chunk, queue_bytes=2 * chunk)
    gaps: list[GapEvent] = []
    blocks = 0
    session.start()
    try:
        assert transport.burst_served.wait(5.0), "the reader never worked through the burst"
        # The transport is silent now, so nothing further can be dropped and
        # this total is final for the rest of the test.
        dropped = session.dropped_bytes_total
        assert dropped > 0, "the burst must overflow the budget or this proves nothing"
        # One more read: the drop marker is handed over alongside the next
        # successful reserve, never on its own.
        transport.resume()
        for event in session.events(idle_timeout_s=2.0):
            if isinstance(event, GapEvent):
                gaps.append(event)
            else:
                blocks += 1
            if gaps and blocks >= 3:
                break
    finally:
        session.stop()
    assert [g.reason for g in gaps] == ["host_overflow"]
    assert sum(g.missing for g in gaps) * 4 == dropped
    # Device-side loss inside the discarded run is unknowable, so the gap
    # cannot claim to be an exact account of what happened there.
    assert all(g.ambiguous for g in gaps)


def test_a_budget_smaller_than_one_read_is_not_a_black_hole():
    """A read larger than the whole budget must still get through.

    The budget is a back-pressure target, not a filter. Refusing every read
    that could not fit meant the consumer saw nothing at all, the parser was
    never told (the drop marker only rides along with a successful reserve),
    and the idle timeout then blamed the device for going quiet while the host
    was discarding 100% of the stream.
    """
    chunk = _FramedFloodTransport.CHUNK_BYTES
    session = StreamSession(_FramedFloodTransport(), read_chunk=chunk, queue_bytes=chunk // 4)
    gaps: list[GapEvent] = []
    stored = 0
    session.start()
    try:
        time.sleep(0.05)  # occupy the queue, so the next read really is refused
        for event in session.events(sample_limit=4 * chunk, idle_timeout_s=1.0):
            if isinstance(event, GapEvent):
                gaps.append(event)
            else:
                stored += len(event.words)
    finally:
        session.stop()
    assert stored > 0, "the whole stream was discarded and no event ever reached the consumer"
    assert any(g.reason == "host_overflow" for g in gaps), "the loss must still be reported"
    # Exactly one read past the budget is admitted -- the overshoot is bounded
    # and deliberate, not an abandoned limit.
    assert session.peak_queued_bytes == chunk


# -- actions scheduled inside a capture ----------------------------------
#
# Switching DUT power mid-capture is how an inrush is recorded, and doing it
# from a timer thread races the reader on the same port and lands within a
# scheduler quantum of where it was asked to. Firing between two blocks is
# single-threaded and lands on a sample index the manifest can record.


def _artifact_writer_threads() -> int:
    return sum(t.name == "ppk2lab-artifact-writer" for t in threading.enumerate())


def test_a_scheduled_action_fires_inline_and_is_recorded():
    fired: list[str] = []
    device = PPK2.open(transport=MockTransport(SimulatedPPK2()), simulate=True)
    try:
        result = device.capture(
            duration_s=0.5, at=[(0.2, lambda: fired.append("on"), "dut_power_on")]
        )
    finally:
        device.close()
    assert fired == ["on"]
    (record,) = result.scheduled_actions
    assert record["label"] == "dut_power_on"
    assert record["error"] is None
    assert record["requested_s"] == pytest.approx(0.2)
    # Within one block of the request, not one scheduler quantum.
    assert record["fired_s"] == pytest.approx(0.2, abs=0.01)
    assert record["fired_index"] == pytest.approx(20_000, abs=1000)


def test_a_failing_action_does_not_cost_the_capture():
    def boom():
        raise RuntimeError("the DUT said no")

    device = PPK2.open(transport=MockTransport(SimulatedPPK2()), simulate=True)
    try:
        result = device.capture(duration_s=0.3, at=[(0.1, boom, "boom")])
    finally:
        device.close()
    assert result.complete, "a bad callback must not damage the recording"
    assert result.stats.stored_samples > 0
    (record,) = result.scheduled_actions
    assert "RuntimeError" in record["error"]
    assert "W_SCHEDULED_ACTION" in _codes(result.warnings)


@pytest.mark.parametrize(
    "entry",
    [
        (0.1,),
        (0.1, "not callable"),
        ("soon", lambda: None),
        (-1.0, lambda: None),
        # Infinity satisfies `delay_s >= 0`, so it used to sail past the
        # validation and reach round() as a bare OverflowError -- no code, no
        # remediation, and nothing to tell a caller which entry was wrong.
        (float("inf"), lambda: None),
        (float("nan"), lambda: None),
    ],
)
def test_a_malformed_schedule_is_refused_before_the_capture(entry, tmp_path):
    """A rejected schedule must cost nothing at all.

    The validation used to run after ``ArtifactWriter`` had opened its temp
    file and started its writer thread, and the raise escaped above the
    try/except that aborts the writer: twenty rejected calls left twenty
    parked threads, twenty open descriptors and twenty ``.tmp`` files, on a
    call that never recorded a sample.
    """
    writers_before = _artifact_writer_threads()
    device = PPK2.open(transport=MockTransport(SimulatedPPK2()), simulate=True)
    try:
        with pytest.raises(UsageError):
            device.capture(duration_s=0.05, output=str(tmp_path / "cap.ppk2a"), at=[entry])
    finally:
        device.close()
    assert list(tmp_path.iterdir()) == [], "a rejected capture left a file behind"
    assert _artifact_writer_threads() <= writers_before


def test_an_action_the_capture_never_reached_is_recorded_rather_than_dropped(tmp_path):
    """A stimulus that never happened is a fact about the run.

    Dropping the entry made a capture whose DUT was never switched on
    byte-for-byte indistinguishable from one that scheduled nothing, so a
    reader could only conclude the load really was that flat.
    """
    fired: list[str] = []
    path = tmp_path / "never.ppk2a"
    device = PPK2.open(transport=MockTransport(SimulatedPPK2()), simulate=True)
    try:
        result = device.capture(
            duration_s=0.3, output=str(path), at=[(5.0, lambda: fired.append("late"), "never")]
        )
    finally:
        device.close()
    assert fired == []
    (record,) = result.scheduled_actions
    assert record["label"] == "never"
    assert record["requested_s"] == pytest.approx(5.0)
    assert record["fired_index"] is None
    assert record["fired_s"] is None
    assert record["error"] is not None
    # The record is only half of it: a reader who did not pass `at=` never
    # looks at scheduled_actions, and the warning is what reaches them.
    assert "W_SCHEDULED_ACTION" in _codes(result.warnings)

    with zipfile.ZipFile(path) as zf:
        manifest = json.loads(zf.read("manifest.json"))
    assert manifest["scheduled_actions"] == result.scheduled_actions
    assert any(w["code"] == "W_SCHEDULED_ACTION" for w in manifest["warnings"])
    # The stored form is the one an agent parses, and null firing fields are
    # new there: a schema that still required integers would reject the very
    # manifest ppk2lab now writes.
    import jsonschema

    from ppk2lab.schemas import get_schema

    jsonschema.validate(manifest, get_schema("capture-manifest"))


def _never_firing_trigger(device):
    """A current threshold no simulated load can cross.

    A trigger that never fires is exactly the case the stream keeps flowing
    and nothing reaches the sink, which is where the capture spends its time
    on a real bench.
    """
    from ppk2lab.triggers.engine import CurrentThresholdTrigger, TriggerEngine

    return TriggerEngine(
        CurrentThresholdTrigger(1e9, direction="above"),
        pre_samples=10,
        post_samples=10,
        calibration=device.calibration,
        vdd_mv=device.state.source_voltage_mv,
    )


def test_a_scheduled_action_cannot_be_combined_with_a_trigger():
    """The two ways of placing an event on the timeline do not compose.

    A triggered capture's timeline begins ``pre_samples`` before the trigger
    fires, so a delay measured from the first sample cannot be honoured. Worse
    in practice: the action is the stimulus meant to make the trigger fire, so
    accepting the pair produced a capture that waited for something its own
    unfired schedule was supposed to cause.
    """
    fired: list[str] = []
    device = PPK2.open(transport=MockTransport(SimulatedPPK2()), simulate=True)
    try:
        with pytest.raises(UsageError):
            device.capture(
                trigger_engine=_never_firing_trigger(device),
                trigger_timeout_s=0.5,
                at=[(0.05, lambda: fired.append("power_on"), "power_on")],
            )
    finally:
        device.close()
    assert fired == [], "the refusal must come before anything touches the DUT"


def test_progress_is_reported_while_a_trigger_has_not_fired():
    """Waiting for a trigger is when a caller most needs to be told anything.

    Progress used to be reported from the sink, and a triggered capture only
    reaches the sink through trigger output -- so a 90 s wait for an event
    that never came produced not one update, the exact silence the callback
    exists to prevent.
    """
    calls: list[dict] = []
    device = PPK2.open(transport=MockTransport(SimulatedPPK2()), simulate=True)
    try:
        result = device.capture(
            trigger_engine=_never_firing_trigger(device),
            trigger_timeout_s=0.6,
            on_progress=calls.append,
        )
    finally:
        device.close()
    assert result.interruption == {"reason": "trigger_timeout"}
    assert len(calls) >= 1
    # Nothing is stored before a trigger fires; reporting anything else here
    # would be reporting samples that no capture will ever contain.
    assert all(update["stored"] == 0 for update in calls)


def test_progress_updates_are_throttled_and_carry_a_stable_payload():
    calls: list[dict] = []
    device = PPK2.open(transport=MockTransport(SimulatedPPK2()), simulate=True)
    started = time.monotonic()
    try:
        result = device.capture(duration_s=2.0, on_progress=calls.append)
    finally:
        device.close()
    elapsed = time.monotonic() - started
    assert result.complete
    assert len(calls) >= 2
    assert all(
        set(update) == {"stored", "elapsed_s", "gap_count", "sample_limit"} for update in calls
    )
    stored = [update["stored"] for update in calls]
    assert stored == sorted(stored), "a progress bar cannot be allowed to run backwards"
    times = [update["elapsed_s"] for update in calls]
    assert times == sorted(times)
    # At most one report per interval, plus the one that fires immediately.
    # The simulator delivers this capture in ~50 blocks, so an unthrottled
    # callback runs ~50 times however slow the host is -- the bound catches a
    # deleted throttle without pinning any particular timing.
    assert len(calls) <= 2 + math.ceil(elapsed / PROGRESS_INTERVAL_S)


def test_a_failing_progress_callback_is_disabled_not_fatal():
    calls: list[dict] = []

    def cb(update):
        calls.append(update)
        raise ValueError("callback is broken")

    device = PPK2.open(transport=MockTransport(SimulatedPPK2()), simulate=True)
    try:
        result = device.capture(duration_s=0.3, on_progress=cb)
    finally:
        device.close()
    assert len(calls) == 1, "a broken callback must not be called again"
    assert "W_PROGRESS_CALLBACK" in _codes(result.warnings)
    assert result.complete
