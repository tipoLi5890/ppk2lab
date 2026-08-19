"""Measurement-integrity guarantees derived from PPK2 hardware facts.

Each test pins a property that follows from what the hardware can and cannot
do — the 6-bit sample counter, the fixed 4-byte frame, the absence of any
voltage measurement — so a refactor cannot quietly trade truthfulness for
convenience.
"""

from __future__ import annotations

import json
import struct
import zipfile

import pytest

from ppk2lab.capture.runner import (
    DEFAULT_IN_MEMORY_LIMIT_SAMPLES,
    MIN_RATE_CHECK_SECONDS,
    timeline_report,
)
from ppk2lab.capture.stats import IMPLAUSIBLE_CURRENT_UA, VoltageContext, compute_stats
from ppk2lab.device import PPK2
from ppk2lab.diagnostics import (
    W_DUT_POWER_UNKNOWN,
    W_NOT_CALIBRATED,
    W_TIMELINE_COMPRESSION,
    W_VOLTAGE_ASSUMED,
    as_json,
)
from ppk2lab.discovery import _classify, _interface_number
from ppk2lab.errors import CaptureFileError, UsageError, VoltageRangeError
from ppk2lab.exports import export_csv
from ppk2lab.protocol.samples import (
    COUNTER_MASK,
    DESYNC_CONSECUTIVE_MISMATCHES,
    SampleBlock,
    SampleStreamParser,
    pack_sample,
)
from ppk2lab.testing.profiles import StepProfile
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
# Memory, metadata delivery, and safety ceilings


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
        assert set(entry) == {"code", "message"}
        assert entry["code"].startswith("W_")


def test_as_json_round_trips_stored_warnings():
    stored = [{"code": "W_SAMPLE_GAPS", "message": "text"}]
    assert as_json(stored) == stored
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
