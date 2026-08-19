"""High-level capture orchestration.

Runs a device stream into an in-memory capture and/or a streaming artifact
writer, with optional triggering. Interruptions (USB failure, Ctrl-C, stall)
never discard data: whatever was captured is preserved with an interruption
record and ``complete=False``.
"""

from __future__ import annotations

import contextlib
import signal
import time
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
    W_SAMPLE_GAPS,
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
from .model import Capture, CaptureBuilder, CaptureMeta
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

    def to_json(self) -> dict[str, Any]:
        return {
            "capture_id": self.capture_id,
            "capture_sha256": self.capture_sha256,
            "path": self.path,
            "complete": self.complete,
            "interruption": self.interruption,
            "trigger": self.trigger,
            "timeline": dict(self.timeline),
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

    started_utc = datetime.now(UTC).isoformat(timespec="milliseconds")
    started = time.monotonic()

    # SIGTERM does not raise, so without this a terminated capture leaves a
    # half-written temp file with no manifest — the whole recording lost
    # rather than preserved as incomplete.
    def _on_terminate(signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt(f"signal {signum}")

    previous_handler: Any = None
    try:
        previous_handler = signal.signal(signal.SIGTERM, _on_terminate)
    except (ValueError, OSError):
        previous_handler = None  # not on the main thread; the caller decides
    try:
        if trigger_engine is None:
            for event in device.stream(sample_limit=sample_limit, duration_s=None):
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
        interruption = {"reason": "keyboard_interrupt"}
        warnings.append(warn(W_INTERRUPTED, "capture interrupted; partial data preserved"))
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

        complete = reached_target and interruption is None and acc.gap_count == 0

        capture: Capture | None = None
        if builder is not None:
            builder.warnings.extend(warnings)
            capture = builder.finish(
                complete=reached_target and interruption is None,
                interruption=interruption,
                timing=timeline,
            )

        path: str | None = None
        sha256: str | None = None
        if writer is not None:
            manifest = writer.finalize(
                complete=reached_target and interruption is None,
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
