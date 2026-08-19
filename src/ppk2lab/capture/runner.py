"""High-level capture orchestration.

Runs a device stream into an in-memory capture and/or a streaming artifact
writer, with optional triggering. Interruptions (USB failure, Ctrl-C, stall)
never discard data: whatever was captured is preserved with an interruption
record and ``complete=False``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ..errors import TransportError
from ..protocol.samples import GapEvent, SampleBlock
from ..triggers.engine import TriggerEngine
from ..types import SAMPLE_RATE_HZ
from .model import Capture, CaptureBuilder, CaptureMeta
from .stats import StatsAccumulator, WindowStats


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
    warnings: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "capture_id": self.capture_id,
            "capture_sha256": self.capture_sha256,
            "path": self.path,
            "complete": self.complete,
            "interruption": self.interruption,
            "trigger": self.trigger,
            "stats": self.stats.to_json() if self.stats else None,
            "warnings": list(self.warnings),
        }


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
) -> CaptureResult:
    """Capture from an open device. Never enables DUT power or changes any
    hardware state other than starting/stopping the measurement stream."""
    from .artifact import ArtifactWriter

    if duration_s is None and sample_limit is None and trigger_engine is None:
        raise ValueError("one of duration_s, sample_limit, or a trigger is required")
    if keep_in_memory is None:
        keep_in_memory = output is None

    state = device.state
    meta = CaptureMeta(
        device=device.info.to_json(),
        configuration={
            "mode": state.mode.name.lower() if state.mode else None,
            "source_voltage_mv": state.source_voltage_mv,
            "dut_power": state.dut_power,
            "sample_rate_hz": SAMPLE_RATE_HZ,
            "digital_channels": [f"D{i}" for i in range(8)],
            "requested": {"duration_s": duration_s, "sample_limit": sample_limit},
            "trigger": trigger_engine.describe() if trigger_engine else None,
        },
        metadata_text=device.metadata.raw_text if device.metadata else None,
    )

    calibration = device.calibration
    vdd = state.source_voltage_mv
    builder = CaptureBuilder(meta) if keep_in_memory else None
    writer = ArtifactWriter(output, meta, overwrite=overwrite) if output else None
    acc = StatsAccumulator(0)
    warnings: list[str] = []
    interruption: dict[str, Any] | None = None
    reached_target = False
    first_index: int | None = None

    if duration_s is not None:
        limit = round(duration_s * SAMPLE_RATE_HZ)
        sample_limit = min(sample_limit, limit) if sample_limit else limit

    def sink(event: SampleBlock | GapEvent) -> None:
        nonlocal first_index
        if isinstance(event, SampleBlock):
            if first_index is None:
                first_index = event.start_index
                acc.start_index = event.start_index
                acc.end_index = event.start_index
            if calibration is not None:
                acc.add_block(event, calibration.convert_block(event, vdd))
        else:
            acc.add_gap(event)
        if builder is not None:
            builder.add(event)
        if writer is not None:
            if isinstance(event, SampleBlock):
                writer.add_block(event)
            else:
                writer.add_gap(event)

    started = time.monotonic()
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
                        f"trigger did not fire within {trigger_timeout_s} s; capture aborted"
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
        warnings.append("capture interrupted by user; partial data preserved")
    except TransportError as exc:
        interruption = {"reason": "transport_error", "detail": exc.message}
        warnings.append(f"capture interrupted: {exc.message}; partial data preserved")
    except BaseException:
        if writer is not None:
            writer.abort()
        raise

    if trigger_engine is not None and not trigger_engine.fired and interruption is None:
        warnings.append("stream ended before the trigger fired")
        interruption = {"reason": "trigger_never_fired"}

    complete = reached_target and interruption is None and acc.gap_count == 0
    if acc.gap_count:
        warnings.append(f"capture contains {acc.gap_count} sample gap(s); see the gap table")
    stats = acc.finalize(source_voltage_mv=vdd) if calibration is not None else None

    capture: Capture | None = None
    if builder is not None:
        builder.warnings.extend(warnings)
        capture = builder.finish(
            complete=reached_target and interruption is None, interruption=interruption
        )

    path: str | None = None
    sha256: str | None = None
    if writer is not None:
        manifest = writer.finalize(
            complete=reached_target and interruption is None,
            interruption=interruption,
            stats=stats.to_json() if stats else None,
            warnings=warnings,
        )
        path = str(writer.path)
        sha256 = manifest["samples"]["sha256"]
    elif capture is not None:
        sha256 = capture.sha256()

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
    )
