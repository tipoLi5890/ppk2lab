"""High-level capture orchestration.

Runs a device stream into an in-memory capture and/or a streaming artifact
writer, with optional triggering. Interruptions (USB failure, Ctrl-C, stall)
never discard data: whatever was captured is preserved with an interruption
record and ``complete=False``.
"""

from __future__ import annotations

import contextlib
import math
import signal
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..diagnostics import (
    W_CALIBRATION_INCOMPLETE,
    W_DUT_POWER_UNKNOWN,
    W_IMPLAUSIBLE_SAMPLES,
    W_INTERRUPTED,
    W_METADATA,
    W_NO_SAMPLES,
    W_NOT_CALIBRATED,
    W_PROGRESS_CALLBACK,
    W_SAMPLE_GAPS,
    W_SCHEDULED_ACTION,
    W_STREAM_DESYNC,
    W_TIMELINE_COMPRESSION,
    W_TRIGGER,
    W_USER_GAIN,
    W_VOLTAGE_ASSUMED,
    Diagnostic,
    warn,
)
from ..errors import StreamStalledError, TransportError, UsageError
from ..protocol.samples import GapEvent, SampleBlock
from ..triggers.engine import TriggerEngine
from ..types import SAMPLE_PERIOD_S, SAMPLE_RATE_HZ, VoltageBasis
from .model import (
    Capture,
    CaptureBuilder,
    CaptureMeta,
    capture_is_complete,
    normalize_user_tags,
)
from .stats import StatsAccumulator, VoltageContext, WindowStats

#: A capture whose timeline advanced measurably slower than the wall clock
#: lost samples the 6-bit counter could not report. The threshold is
#: deliberately loose: USB delivery is bursty, and a straggling final block
#: must never be reported as data loss. A host starved badly enough to alias
#: the counter drops a large fraction of the stream, so a 10% deficit still
#: detects it with a wide margin. The exact ratio is always reported, so
#: tighter analysis stays possible downstream.
RATE_DEFICIT_TOLERANCE = 0.10
#: Below this observation window, delivery jitter dominates and the check
#: reports ``too_short`` instead of guessing.
MIN_RATE_CHECK_SECONDS = 2.0
#: Refuse to buffer more than this in RAM without an explicit opt-in
#: (4 bytes/sample: 60 s is ~24 MB, an 8 h capture would be ~11.5 GB).
DEFAULT_IN_MEMORY_LIMIT_SAMPLES = 60 * SAMPLE_RATE_HZ
#: The host clock and the device's sample clock are independent oscillators
#: whose relative rate this project has never measured (a comparison against a
#: disciplined reference is a soak-gate item). 500 ppm sits above the tolerance
#: of any crystal a USB device ships with, so a shortfall inside it is
#: arithmetic on two clocks rather than evidence of loss.
UNMEASURED_CLOCK_TOLERANCE = 5e-4
#: Either wall-clock anchor can sit behind the samples it marks: the stamp is
#: taken when a whole USB block has been delivered *and* consumed, and the
#: reader polls the transport on a bounded timeout. One poll interval per
#: anchor is the floor; the block spans are added on top, and both the raw
#: estimate and this floor are reported so a hardware session can replace the
#: assumption with a measurement.
ANCHOR_JITTER_S = 0.05


@dataclass
class CaptureResult:
    capture: Capture | None
    path: str | None
    stats: WindowStats | None
    complete: bool
    interruption: dict[str, Any] | None
    trigger: dict[str, Any] | None
    capture_id: str
    capture_sha256: str | None
    warnings: list[Diagnostic] = field(default_factory=list)
    #: Wall-clock cross-check of the sample timeline; see ``timeline_report``.
    timeline: dict[str, Any] = field(default_factory=dict)
    #: Scheduled actions, with the sample index each fired at — or nulls and a
    #: reason for one the capture ended before reaching.
    scheduled_actions: list[dict[str, Any]] = field(default_factory=list)
    #: The provenance the caller passed in, handed straight back. A caller that
    #: just tagged a capture should not have to reopen the file to read them.
    user_tags: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "capture_id": self.capture_id,
            "capture_sha256": self.capture_sha256,
            "path": self.path,
            "complete": self.complete,
            "interruption": self.interruption,
            "trigger": self.trigger,
            "timeline": dict(self.timeline),
            "user_tags": dict(self.user_tags),
            "scheduled_actions": list(self.scheduled_actions),
            "stats": self.stats.to_json() if self.stats else None,
            "warnings": [w.to_json() for w in self.warnings],
        }


def timeline_report(
    *,
    first_sample_at: float | None,
    last_sample_at: float | None,
    timeline_advance: int,
    started_utc: str,
    ended_utc: str,
    applicable: bool = True,
    first_sample_utc: str | None = None,
    first_block_samples: int = 0,
    last_block_samples: int = 0,
) -> dict[str, Any]:
    """Compare the sample timeline against the host's wall clock.

    The 6-bit sample counter can only describe losses smaller than 64 samples;
    a larger burst aliases, and a loss of exactly k*64 samples is invisible to
    it entirely. Wall-clock elapsed time is the only independent witness: if
    N samples arrived over T seconds and N/T is far below 100 kS/s, samples
    went missing however quiet the counter stayed.

    Timing is measured between the first and last received samples, so the
    device's start-up latency is not mistaken for loss. The two anchors are
    stamped when a whole USB block reaches the host, not when its samples were
    taken, so the interval is uncertain by the span of those two blocks; that
    span, plus an allowance for two unsynchronized clocks, is the floor below
    which a shortfall is not evidence of anything.
    """
    report: dict[str, Any] = {
        "started_utc": started_utc,
        "ended_utc": ended_utc,
        "first_sample_utc": first_sample_utc,
        "anchor_uncertainty_s": None,
        "wall_elapsed_s": None,
        "timeline_advance": timeline_advance,
        "achieved_sample_rate_hz": None,
        "rate_deficit_ratio": None,
        "rate_offset_ratio": None,
        "unaccounted_samples_estimate": None,
        "unaccounted_floor_samples": None,
        "rate_check": "not_applicable",
    }
    if not applicable or first_sample_at is None or last_sample_at is None:
        return report
    if first_block_samples:
        # How much earlier than `first_sample_utc` the first sample was taken.
        report["anchor_uncertainty_s"] = first_block_samples * SAMPLE_PERIOD_S
    elapsed = last_sample_at - first_sample_at
    report["wall_elapsed_s"] = elapsed
    if elapsed <= 0:
        return report
    achieved = timeline_advance / elapsed
    report["achieved_sample_rate_hz"] = achieved
    report["rate_deficit_ratio"] = max(0.0, 1.0 - achieved / SAMPLE_RATE_HZ)
    # The unclamped companion: positive means the timeline advanced faster than
    # wall time, which is an anchoring artifact rather than a measurement, and
    # a clamped figure hides it.
    report["rate_offset_ratio"] = achieved / SAMPLE_RATE_HZ - 1.0
    expected = round(elapsed * SAMPLE_RATE_HZ)
    report["unaccounted_samples_estimate"] = expected - timeline_advance
    report["unaccounted_floor_samples"] = (
        first_block_samples
        + last_block_samples
        + round(2 * ANCHOR_JITTER_S * SAMPLE_RATE_HZ)
        + round(UNMEASURED_CLOCK_TOLERANCE * expected)
    )
    if elapsed < MIN_RATE_CHECK_SECONDS:
        report["rate_check"] = "too_short"
    elif report["rate_deficit_ratio"] > RATE_DEFICIT_TOLERANCE:
        report["rate_check"] = "deficit"
    else:
        report["rate_check"] = "ok"
    return report


#: How often a progress callback may be invoked. Progress is a courtesy to a
#: human watching a long capture, not a data path; calling it per block would
#: put a caller's code in the way of the stream 25 times a second.
PROGRESS_INTERVAL_S = 0.25


def _normalize_schedule(at: Sequence[tuple[Any, ...]] | None) -> list[tuple[int, Any, str]]:
    """Validate scheduled actions and put them in firing order.

    Each entry is ``(delay_s, callable)`` or ``(delay_s, callable, label)``,
    with ``delay_s`` measured from the first sample of the capture.
    """
    if not at:
        return []
    out: list[tuple[int, Any, str]] = []
    for i, entry in enumerate(at):
        if not isinstance(entry, tuple) or len(entry) not in (2, 3):
            raise UsageError(
                f"scheduled action {i} must be (delay_s, callable[, label]), got {entry!r}"
            )
        delay_s, action = entry[0], entry[1]
        label = entry[2] if len(entry) == 3 else getattr(action, "__name__", f"action{i}")
        if not callable(action):
            raise UsageError(f"scheduled action {label!r} is not callable")
        if not isinstance(delay_s, int | float) or isinstance(delay_s, bool):
            raise UsageError(f"scheduled action {label!r} needs a numeric delay_s")
        # isfinite rather than `>= 0`: infinity satisfies `>= 0` and would
        # reach round() as a bare OverflowError, where every other malformed
        # entry here produces a UsageError with a remediation.
        if not math.isfinite(delay_s) or delay_s < 0:
            raise UsageError(
                f"scheduled action {label!r} needs a finite delay_s >= 0, got {delay_s!r}"
            )
        if not isinstance(label, str):
            raise UsageError(f"scheduled action {i} needs a string label, got {label!r}")
        out.append((round(delay_s * SAMPLE_RATE_HZ), action, label))
    out.sort(key=lambda item: item[0])
    return out


def run_capture(
    device: Any,
    *,
    duration_s: float | None = None,
    sample_limit: int | None = None,
    output: str | None = None,
    overwrite: bool = False,
    keep_in_memory: bool | None = None,
    trigger_engine: TriggerEngine | None = None,
    trigger_timeout_s: float | None = None,
    assume_voltage_mv: int | None = None,
    in_memory_limit_samples: int | None = DEFAULT_IN_MEMORY_LIMIT_SAMPLES,
    tags: dict[str, str] | None = None,
    at: Sequence[tuple[Any, ...]] | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    on_block: Callable[[SampleBlock | GapEvent], None] | None = None,
) -> CaptureResult:
    """Capture from an open device. Never enables DUT power or changes any
    hardware state other than starting/stopping the measurement stream."""
    from .artifact import ArtifactWriter

    if duration_s is None and sample_limit is None and trigger_engine is None:
        raise UsageError(
            "one of duration_s, sample_limit, or a trigger is required",
            remediation="A capture needs a stopping condition: pass duration_s=, "
            "sample_limit=, or a trigger engine (`--duration`, `--samples`, `--trigger`).",
        )
    if keep_in_memory is None:
        keep_in_memory = output is None

    # Validated here, with the other argument checks, and deliberately before
    # ArtifactWriter opens a temp file and starts its writer thread: a raise
    # below that point escapes above the try/except that would abort the
    # writer, leaving a parked thread, an open fd and a stray `.tmp` file
    # behind for every rejected call.
    pending_actions = _normalize_schedule(at)
    if at and trigger_engine is not None:
        raise UsageError(
            "scheduled actions cannot be combined with a trigger",
            remediation="A triggered capture's timeline starts pre_samples before the "
            "trigger fires, so a delay measured from the first sample cannot be honoured "
            "and the recorded fired_index would predate the action. Run the stimulus "
            "before the capture, or use duration_s=/sample_limit=.",
        )

    if duration_s is not None:
        limit = round(duration_s * SAMPLE_RATE_HZ)
        sample_limit = min(sample_limit, limit) if sample_limit else limit

    if keep_in_memory and in_memory_limit_samples is not None:
        projected = sample_limit
        if trigger_engine is not None:
            projected = trigger_engine.pre_samples + trigger_engine.post_samples
        if projected is None or projected > in_memory_limit_samples:
            budget_s = in_memory_limit_samples / SAMPLE_RATE_HZ
            requested = "unbounded" if projected is None else f"{projected / SAMPLE_RATE_HZ:g} s"
            raise UsageError(
                f"refusing to buffer a {requested} capture in memory "
                f"(limit {budget_s:g} s, about {in_memory_limit_samples * 4 / 1e6:.0f} MB)",
                remediation="Pass an output path so samples stream to disk "
                "(`--output run.ppk2a`), or raise in_memory_limit_samples deliberately.",
            )

    state = device.state
    voltage = VoltageContext(
        voltage_mv=assume_voltage_mv if assume_voltage_mv is not None else state.source_voltage_mv,
        basis=(
            VoltageBasis.CALLER_OVERRIDE.value
            if assume_voltage_mv is not None
            else state.source_voltage_basis.value
        ),
        mode=state.mode.name.lower() if state.mode else None,
    )
    device_block = device.info.to_json()
    if hasattr(device, "firmware_fingerprint"):
        # Captures must be traceable to the firmware that produced them, and
        # the measurement port never reports a version string.
        device_block["firmware_fingerprint"] = device.firmware_fingerprint()
    meta = CaptureMeta(
        device=device_block,
        configuration={
            "mode": voltage.mode,
            "source_voltage_mv": state.source_voltage_mv,
            "voltage_basis": voltage.basis,
            "voltage_measured": False,
            "assumed_voltage_mv": assume_voltage_mv,
            "dut_power": state.dut_power,
            "sample_rate_hz": SAMPLE_RATE_HZ,
            "digital_channels": [f"D{i}" for i in range(8)],
            "requested": {"duration_s": duration_s, "sample_limit": sample_limit},
            "trigger": trigger_engine.describe() if trigger_engine else None,
        },
        metadata_text=device.metadata.raw_text if device.metadata else None,
        # Validated before a single sample is recorded: a tag rejected at
        # finalize would cost the capture, and bench time is not re-acquirable.
        user_tags=normalize_user_tags(tags),
    )

    calibration = device.calibration
    # The calibration correction term uses the device's own VDD field in every
    # mode; only the energy figure depends on which voltage is defensible.
    vdd = state.source_voltage_mv
    builder = CaptureBuilder(meta) if keep_in_memory else None
    writer = ArtifactWriter(output, meta, overwrite=overwrite) if output else None
    acc = StatsAccumulator(0)
    warnings: list[Diagnostic] = list(_calibration_warnings(device))
    if state.dut_power is not True:
        warnings.append(
            warn(
                W_DUT_POWER_UNKNOWN,
                "DUT power state is unknown or off (the device cannot report it); if the "
                "DUT is not powered through the meter, the capture will read near zero",
            )
        )
    note = voltage.note()
    if note and not voltage.energy_defensible:
        warnings.append(warn(W_VOLTAGE_ASSUMED, note))
    interruption: dict[str, Any] | None = None
    reached_target = False
    first_index: int | None = None
    last_index: int | None = None
    first_sample_at: float | None = None
    last_sample_at: float | None = None
    first_sample_utc: str | None = None
    first_block_samples = 0
    last_block_samples = 0

    fired_actions: list[dict[str, Any]] = []
    progress_state = {"last": 0.0, "live": on_progress is not None}
    observer_state = {"live": on_block is not None}

    def _run_due_actions() -> None:
        """Fire scheduled actions inline, on this thread.

        Inline is the point. A timer thread writing to the same serial port
        races the reader and lands within a scheduler quantum of where it was
        asked to; here the action happens between two blocks, at a sample
        index the manifest can then record.
        """
        if not pending_actions or first_index is None or last_index is None:
            return
        elapsed_samples = last_index - first_index
        while pending_actions and pending_actions[0][0] <= elapsed_samples:
            due_samples, action, label = pending_actions.pop(0)
            record: dict[str, Any] = {
                "label": label,
                "requested_s": due_samples / SAMPLE_RATE_HZ,
                "fired_index": last_index,
                "fired_s": elapsed_samples / SAMPLE_RATE_HZ,
                "error": None,
            }
            try:
                action()
            except Exception as exc:
                # The capture outlives a bad callback: samples already taken
                # cannot be re-acquired, and a silent failure would leave the
                # manifest claiming something happened that did not.
                record["error"] = f"{type(exc).__name__}: {exc}"
                warnings.append(
                    warn(
                        W_SCHEDULED_ACTION,
                        f"scheduled action {label!r} raised at "
                        f"{record['fired_s']:.3f} s: {record['error']}",
                    )
                )
            fired_actions.append(record)

    def _report_progress() -> None:
        if not progress_state["live"] or on_progress is None:
            return
        now = time.monotonic()
        if now - float(progress_state["last"]) < PROGRESS_INTERVAL_S:
            return
        progress_state["last"] = now
        try:
            on_progress(
                {
                    "stored": acc.stored,
                    "elapsed_s": now - started,
                    "gap_count": acc.gap_count,
                    "sample_limit": sample_limit,
                }
            )
        except Exception as exc:
            # A reporting callback is not worth a capture. Say so once.
            progress_state["live"] = False
            warnings.append(
                warn(W_PROGRESS_CALLBACK, f"progress callback raised and was disabled: {exc}")
            )

    def _observe(event: SampleBlock | GapEvent) -> None:
        """Hand the caller the same events the artifact receives.

        Unlike ``on_progress`` this is per block and not throttled: it exists
        so a live view can be fed from the capture itself rather than from a
        second stream, and a second stream is not available -- one device owns
        one stream. That makes it a data path, so the backpressure warning on
        ``on_progress`` and ``at=`` applies here with more force: whatever this
        callback does happens between two reads of a 100 kS/s device, and time
        spent inside it is time the reader thread's queue is filling.

        The events are the same objects the writer stores. Treat them as
        read-only; mutating one would change what the artifact records.
        """
        if not observer_state["live"] or on_block is None:
            return
        try:
            on_block(event)
        except Exception as exc:
            # A watcher is not worth a capture. Say so once and keep going --
            # the artifact is the deliverable, the live view is not.
            observer_state["live"] = False
            warnings.append(
                warn(W_PROGRESS_CALLBACK, f"block callback raised and was disabled: {exc}")
            )

    def sink(event: SampleBlock | GapEvent) -> None:
        nonlocal first_index, last_index, first_sample_at, last_sample_at
        nonlocal first_sample_utc, first_block_samples, last_block_samples
        if isinstance(event, SampleBlock):
            now = time.monotonic()
            if first_index is None:
                first_index = event.start_index
                acc.start_index = event.start_index
                acc.end_index = event.start_index
                first_sample_at = now
                # Stamped on delivery, so the first sample was taken up to one
                # block earlier; `anchor_uncertainty_s` carries that bound.
                first_sample_utc = datetime.now(UTC).isoformat(timespec="milliseconds")
                first_block_samples = len(event)
            last_sample_at = now
            last_block_samples = len(event)
            # Timeline extent is a property of the stream, not of whether the
            # samples could be converted: an uncalibrated device must not look
            # like a stalled one.
            last_index = event.end_index
            if calibration is not None:
                acc.add_block(event, calibration.convert_block(event, vdd))
        else:
            if event.missing is not None and last_index is not None:
                last_index = max(last_index, event.index + event.missing)
            acc.add_gap(event)
        if builder is not None:
            builder.add(event)
        if writer is not None:
            if isinstance(event, SampleBlock):
                writer.add_block(event)
            else:
                writer.add_gap(event)
        _observe(event)
        _run_due_actions()

    started_utc = datetime.now(UTC).isoformat(timespec="milliseconds")
    started = time.monotonic()

    # SIGTERM does not raise, so without this a terminated capture leaves a
    # half-written temp file with no manifest — the whole recording lost
    # rather than preserved as incomplete. The signal is recorded because a
    # process manager killing a CI capture and an operator pressing Ctrl-C are
    # different causes, and only one of them is a person.
    terminated_by: list[int] = []

    def _on_terminate(signum: int, _frame: Any) -> None:
        terminated_by.append(signum)
        raise KeyboardInterrupt(f"signal {signum}")

    previous_handler: Any = None
    try:
        previous_handler = signal.signal(signal.SIGTERM, _on_terminate)
    except (ValueError, OSError):
        previous_handler = None  # not on the main thread; the caller decides
    try:
        if trigger_engine is None:
            for event in device.stream(sample_limit=sample_limit, duration_s=None):
                # Driven by the stream, not by `sink`: a triggered capture
                # reaches `sink` only through trigger output, so reporting from
                # there showed nothing at all during the wait -- which is the
                # 90 s capture the callback exists for.
                _report_progress()
                if (
                    sample_limit is not None
                    and isinstance(event, SampleBlock)
                    and event.end_index > sample_limit
                ):
                    n = sample_limit - event.start_index
                    if n > 0:
                        sink(SampleBlock(event.start_index, event.words[:n]))
                    reached_target = True
                    break
                sink(event)
                if (
                    sample_limit is not None
                    and isinstance(event, SampleBlock)
                    and event.end_index >= sample_limit
                ):
                    reached_target = True
                    break
        else:
            for event in device.stream():
                _report_progress()
                if (
                    trigger_timeout_s is not None
                    and not trigger_engine.fired
                    and time.monotonic() - started > trigger_timeout_s
                ):
                    warnings.append(
                        warn(
                            W_TRIGGER,
                            f"trigger did not fire within {trigger_timeout_s} s; capture aborted",
                        )
                    )
                    interruption = {"reason": "trigger_timeout"}
                    break
                for out in trigger_engine.process(event):
                    sink(out)
                if trigger_engine.done:
                    reached_target = True
                    break
    except KeyboardInterrupt:
        if terminated_by:
            interruption = {
                "reason": "terminated",
                "detail": f"signal {terminated_by[0]}",
            }
            detail = "capture terminated by signal; partial data preserved"
        else:
            interruption = {"reason": "keyboard_interrupt"}
            detail = "capture interrupted; partial data preserved"
        warnings.append(warn(W_INTERRUPTED, detail))
    except StreamStalledError as exc:
        interruption = {"reason": "stream_stalled", "detail": exc.message}
        warnings.append(
            warn(W_INTERRUPTED, f"capture interrupted: {exc.message}; partial data preserved")
        )
    except TransportError as exc:
        interruption = {"reason": "transport_error", "detail": exc.message}
        warnings.append(
            warn(W_INTERRUPTED, f"capture interrupted: {exc.message}; partial data preserved")
        )
    except BaseException:
        if writer is not None:
            writer.abort()
        raise
    finally:
        if previous_handler is not None:
            with contextlib.suppress(ValueError, OSError):
                signal.signal(signal.SIGTERM, previous_handler)

    try:
        if trigger_engine is not None and not trigger_engine.fired and interruption is None:
            warnings.append(warn(W_TRIGGER, "stream ended before the trigger fired"))
            interruption = {"reason": "trigger_never_fired"}

        ended_utc = datetime.now(UTC).isoformat(timespec="milliseconds")
        timeline = timeline_report(
            first_sample_at=first_sample_at,
            last_sample_at=last_sample_at,
            timeline_advance=(
                (last_index - first_index) if first_index is not None and last_index else 0
            ),
            started_utc=started_utc,
            ended_utc=ended_utc,
            # A triggered capture emits its whole pre-trigger ring buffer at the
            # moment the trigger fires, so wall time covers only the post-trigger
            # part while the timeline covers both. Comparing them would be
            # arithmetic on two different intervals.
            applicable=trigger_engine is None,
            first_sample_utc=first_sample_utc,
            first_block_samples=first_block_samples,
            last_block_samples=last_block_samples,
        )
        if timeline["rate_check"] == "deficit":
            deficit = timeline["rate_deficit_ratio"]
            achieved = timeline["achieved_sample_rate_hz"]
            warnings.append(
                warn(
                    W_TIMELINE_COMPRESSION,
                    f"timeline compression: {achieved:,.0f} samples/s reached against a nominal "
                    f"{SAMPLE_RATE_HZ:,} S/s ({deficit:.1%} short). Samples were lost beyond "
                    "what the 6-bit counter can report, so durations and integrals understate "
                    "reality",
                )
            )
            if interruption is None:
                interruption = {
                    "reason": "timeline_compression",
                    "detail": f"achieved {achieved:.0f} S/s, deficit {deficit:.3f}",
                }

        if acc.gap_count:
            warnings.append(
                warn(
                    W_SAMPLE_GAPS,
                    f"capture contains {acc.gap_count} sample gap(s); see the gap table",
                )
            )
        session = getattr(device, "last_session", None)
        parser = getattr(session, "parser", None)
        desync_events = getattr(parser, "desync_events", 0) if parser is not None else 0
        if desync_events:
            warnings.append(
                warn(
                    W_STREAM_DESYNC,
                    f"{desync_events} byte-level framing desync(s) were detected and "
                    "re-aligned; samples around them were discarded rather than reported, "
                    "and the timeline is degraded across each one",
                )
            )
        if acc.implausible:
            warnings.append(
                warn(
                    W_IMPLAUSIBLE_SAMPLES,
                    f"{acc.implausible} sample(s) converted to a physically impossible current "
                    "and were excluded from statistics; this indicates a stream framing desync",
                )
            )
        if acc.stored == 0 and interruption is None:
            warnings.append(
                warn(W_NO_SAMPLES, "capture stored no samples; the device produced no data")
            )

        stats = acc.finalize(voltage=voltage, timing=timeline) if calibration is not None else None
        if stats is not None:
            # Saturation, an unenumerated gap table, and wall-clock loss the
            # counter could not describe are all statements about the data the
            # accumulator just walked; deriving them there keeps the live path and
            # the offline `measure` path from disagreeing about what they mean.
            warnings.extend(stats.diagnostics())

        ran_to_completion = reached_target and interruption is None
        # acc.gap_count counts every gap, including any the sinks' gap tables
        # stopped enumerating, so truncation cannot hide loss from this test.
        complete = capture_is_complete(ran_to_completion=ran_to_completion, gap_count=acc.gap_count)

        # An action that never came due is a fact about the run, and dropping
        # it made a capture whose stimulus never happened byte-for-byte
        # indistinguishable from one that scheduled nothing. Recorded with the
        # ones that fired, and warned about once for the whole set so a long
        # schedule cannot flood the list -- the warning is the half that
        # reaches a reader who did not pass `at=`.
        if pending_actions:
            ended_s = (
                (last_index - first_index) / SAMPLE_RATE_HZ
                if first_index is not None and last_index is not None
                else 0.0
            )
            for due_samples, _action, label in pending_actions:
                fired_actions.append(
                    {
                        "label": label,
                        "requested_s": due_samples / SAMPLE_RATE_HZ,
                        "fired_index": None,
                        "fired_s": None,
                        "error": f"never fired: the capture ended at {ended_s:.3f} s",
                    }
                )
            names = ", ".join(repr(entry[2]) for entry in pending_actions)
            warnings.append(
                warn(
                    W_SCHEDULED_ACTION,
                    f"{len(pending_actions)} scheduled action(s) never fired; the capture "
                    f"ended at {ended_s:.3f} s: {names}",
                )
            )
            pending_actions.clear()

        # Recorded before either sink is finalized so the stored manifest and
        # the in-memory capture describe the same run.
        meta.scheduled_actions = fired_actions

        capture: Capture | None = None
        if builder is not None:
            builder.warnings.extend(warnings)
            capture = builder.finish(
                complete=ran_to_completion,
                interruption=interruption,
                timing=timeline,
            )

        path: str | None = None
        sha256: str | None = None
        if writer is not None:
            manifest = writer.finalize(
                complete=ran_to_completion,
                interruption=interruption,
                stats=stats.to_json() if stats else None,
                warnings=[w.to_json() for w in warnings],
                calibration=_calibration_block(device),
                timing=timeline,
            )
            path = str(writer.path)
            sha256 = manifest["samples"]["sha256"]
        elif capture is not None:
            sha256 = capture.sha256()
    except BaseException:
        # Anything raised between the end of the stream and the rename leaves
        # a temp file with no manifest, which is not a capture. abort() is a
        # no-op once finalize has closed the container, so a complete capture
        # that merely failed to be renamed is preserved rather than deleted.
        if writer is not None:
            writer.abort()
        raise

    return CaptureResult(
        capture=capture,
        path=path,
        stats=stats,
        complete=complete,
        interruption=interruption,
        trigger=trigger_engine.describe() if trigger_engine else None,
        capture_id=meta.capture_id,
        capture_sha256=sha256,
        warnings=warnings,
        timeline=timeline,
        scheduled_actions=fired_actions,
        user_tags=dict(meta.user_tags),
    )


def _calibration_block(device: Any) -> dict[str, Any]:
    """Calibration provenance recorded with every capture."""
    metadata = getattr(device, "metadata", None)
    calibration = getattr(device, "calibration", None)
    return {
        "calibrated_flag": getattr(metadata, "calibrated", None),
        "metadata_terminated": getattr(metadata, "terminated", None),
        "metadata_warnings": list(getattr(metadata, "warnings", []) or []),
        "missing_ranges": calibration.missing_ranges() if calibration else None,
        "user_gains": (
            [None if g is None else g.ug for g in calibration.ranges] if calibration else None
        ),
    }


def _calibration_warnings(device: Any) -> list[Diagnostic]:
    """Surface every reason the calibration chain may be untrustworthy.

    A missing constant is never replaced by a default — the affected range
    converts to NaN instead, because a zero offset alone would bias every
    sample in that range by the full offset. But refusing to invent data is
    only half the job: the capture must also carry the reason its numbers are
    suspect, or the problem is merely postponed to whoever reads it later.
    """
    warnings: list[Diagnostic] = []
    metadata = getattr(device, "metadata", None)
    calibration = getattr(device, "calibration", None)
    if metadata is not None:
        warnings.extend(warn(W_METADATA, text) for text in metadata.warnings)
        if metadata.calibrated is False:
            warnings.append(
                warn(
                    W_NOT_CALIBRATED,
                    "device metadata reports Calibrated: 0; the meaning of this flag is not "
                    "hardware-verified, so treat absolute accuracy as unconfirmed",
                )
            )
        if not metadata.terminated:
            warnings.append(
                warn(
                    W_METADATA,
                    "device metadata was not terminated by END; calibration constants may "
                    "be incomplete and affected ranges will not convert",
                )
            )
    if calibration is not None:
        missing = calibration.missing_ranges()
        if missing:
            warnings.append(
                warn(
                    W_CALIBRATION_INCOMPLETE,
                    f"calibration constants missing for measurement range(s) {missing}; "
                    "samples in those ranges cannot be converted and are excluded from "
                    "statistics",
                )
            )
        gains = [
            (index, rc.ug)
            for index, rc in enumerate(calibration.ranges)
            if rc is not None and rc.ug != 1.0
        ]
        if gains:
            detail = ", ".join(f"range {i}: {g:g}" for i, g in gains)
            warnings.append(
                warn(
                    W_USER_GAIN,
                    f"non-unity user gain in effect ({detail}); every reading in those "
                    "ranges is scaled by it",
                )
            )
    return warnings
