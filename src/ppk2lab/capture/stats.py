"""Current statistics, charge, energy, peaks, and latency over sample windows.

Charge integrates rectangle-rule at the fixed 10 us sample period:
``charge_uc = sum(current_ua) * 10e-6`` (uA*s == uC). Energy multiplies charge
by the source voltage; when the supply voltage is unknown (for example an
Ampere-mode capture without metadata) energy is reported as ``None``, never
guessed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from ..protocol.samples import GapEvent, SampleBlock
from ..types import SAMPLE_PERIOD_S


@dataclass
class WindowStats:
    start_index: int
    end_index: int
    stored_samples: int = 0
    missing_samples_known: int = 0
    has_unknown_gaps: bool = False
    gap_count: int = 0
    valid_samples: int = 0
    invalid_range_samples: int = 0
    nan_samples: int = 0
    mean_ua: float | None = None
    min_ua: float | None = None
    max_ua: float | None = None
    peak_index: int | None = None
    charge_uc: float | None = None
    energy_uj: float | None = None
    source_voltage_mv: int | None = None
    duration_s: float = 0.0
    complete: bool = True
    gaps: list[dict[str, Any]] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "window": {"start_index": self.start_index, "end_index": self.end_index},
            "duration_s": self.duration_s,
            "samples": {
                "stored": self.stored_samples,
                "missing_known": self.missing_samples_known,
                "has_unknown_gaps": self.has_unknown_gaps,
                "valid": self.valid_samples,
                "invalid_range": self.invalid_range_samples,
                "not_convertible": self.nan_samples,
            },
            "current_ua": {
                "mean": self.mean_ua,
                "min": self.min_ua,
                "max": self.max_ua,
                "peak_index": self.peak_index,
            },
            "charge_uc": self.charge_uc,
            "energy_uj": self.energy_uj,
            "source_voltage_mv": self.source_voltage_mv,
            "complete": self.complete,
            "sample_gaps": self.gaps,
        }


class StatsAccumulator:
    """Online statistics over a stream of calibrated blocks."""

    def __init__(self, start_index: int) -> None:
        self.start_index = start_index
        self.end_index = start_index
        self.stored = 0
        self.missing_known = 0
        self.unknown_gaps = False
        self.gap_count = 0
        self.valid = 0
        self.invalid_range = 0
        self.nans = 0
        self.total = 0.0
        self.min_v: float | None = None
        self.max_v: float | None = None
        self.peak_index: int | None = None
        self.gaps: list[GapEvent] = []

    def add_block(self, block: SampleBlock, currents_ua: list[float]) -> None:
        self.stored += len(block)
        self.end_index = max(self.end_index, block.end_index)
        ranges = block.ranges
        for offset, value in enumerate(currents_ua):
            if ranges[offset] > 4:
                self.invalid_range += 1
                continue
            if math.isnan(value):
                self.nans += 1
                continue
            self.valid += 1
            self.total += value
            if self.min_v is None or value < self.min_v:
                self.min_v = value
            if self.max_v is None or value > self.max_v:
                self.max_v = value
                self.peak_index = block.start_index + offset

    def add_gap(self, gap: GapEvent) -> None:
        self.gaps.append(gap)
        self.gap_count += 1
        if gap.missing is None:
            self.unknown_gaps = True
        else:
            self.missing_known += gap.missing
            self.end_index = max(self.end_index, gap.index + gap.missing)

    def finalize(self, *, source_voltage_mv: int | None = None) -> WindowStats:
        charge = self.total * SAMPLE_PERIOD_S if self.valid else None
        energy = None
        if charge is not None and source_voltage_mv is not None:
            energy = charge * (source_voltage_mv / 1000.0)
        return WindowStats(
            start_index=self.start_index,
            end_index=self.end_index,
            stored_samples=self.stored,
            missing_samples_known=self.missing_known,
            has_unknown_gaps=self.unknown_gaps,
            gap_count=self.gap_count,
            valid_samples=self.valid,
            invalid_range_samples=self.invalid_range,
            nan_samples=self.nans,
            mean_ua=(self.total / self.valid) if self.valid else None,
            min_ua=self.min_v,
            max_ua=self.max_v,
            peak_index=self.peak_index,
            charge_uc=charge,
            energy_uj=energy,
            source_voltage_mv=source_voltage_mv,
            duration_s=(self.end_index - self.start_index) * SAMPLE_PERIOD_S,
            complete=(self.gap_count == 0),
            gaps=[g.to_json() for g in self.gaps],
        )


def compute_stats(
    capture: Any,
    *,
    start_index: int | None = None,
    end_index: int | None = None,
    filtered: bool = False,
) -> WindowStats:
    """Statistics over a capture or a timeline window of it.

    ``start_index``/``end_index`` are timeline indexes; samples inside gaps
    simply do not exist and are counted as missing.
    """
    from ..calibration import SpikeFilter

    calibration = capture.calibration
    if calibration is None:
        raise ValueError("capture has no calibration metadata; cannot compute currents")
    vdd = capture.source_voltage_mv
    lo = capture.start_index if start_index is None else start_index
    hi = capture.end_index if end_index is None else end_index
    acc = StatsAccumulator(lo)
    acc.end_index = hi if end_index is not None else lo
    spike = SpikeFilter() if filtered else None
    for event in capture.iter_events():
        if isinstance(event, GapEvent):
            missing = event.missing or 0
            if spike:
                spike.notify_gap()
            gap_lo, gap_hi = event.index, event.index + missing
            if gap_hi > lo and gap_lo < hi:
                clipped = GapEvent(
                    index=max(gap_lo, lo),
                    missing=(min(gap_hi, hi) - max(gap_lo, lo))
                    if event.missing is not None
                    else None,
                    reason=event.reason,
                    ambiguous=event.ambiguous,
                )
                acc.add_gap(clipped)
            continue
        block = event
        if block.end_index <= lo or block.start_index >= hi:
            continue
        s = max(0, lo - block.start_index)
        e = min(len(block), hi - block.start_index)
        sub = SampleBlock(block.start_index + s, block.words[s:e])
        currents = calibration.convert_block(sub, vdd)
        if spike:
            currents = spike.apply(currents, sub.ranges)
        acc.add_block(sub, currents)
    stats = acc.finalize(source_voltage_mv=vdd)
    if end_index is not None:
        stats.end_index = hi
        stats.duration_s = (hi - lo) * SAMPLE_PERIOD_S
    return stats


def latency_until_below(
    capture: Any,
    *,
    from_index: int,
    threshold_ua: float,
    hold_samples: int = 1,
    max_index: int | None = None,
) -> int | None:
    """Timeline index where current first stays below ``threshold_ua`` for
    ``hold_samples`` consecutive stored samples, starting at ``from_index``.

    Returns None if the condition never holds. A gap resets the hold count —
    missing data never counts as evidence.
    """
    calibration = capture.calibration
    if calibration is None:
        raise ValueError("capture has no calibration metadata")
    vdd = capture.source_voltage_mv
    hold = 0
    hold_start: int | None = None
    hi = max_index if max_index is not None else capture.end_index
    for event in capture.iter_events():
        if isinstance(event, GapEvent):
            hold = 0
            hold_start = None
            continue
        if event.end_index <= from_index:
            continue
        currents = calibration.convert_block(event, vdd)
        for offset, value in enumerate(currents):
            t = event.start_index + offset
            if t < from_index:
                continue
            if t >= hi:
                return None
            if not math.isnan(value) and value < threshold_ua:
                if hold == 0:
                    hold_start = t
                hold += 1
                if hold >= hold_samples:
                    return hold_start
            else:
                hold = 0
                hold_start = None
    return None
