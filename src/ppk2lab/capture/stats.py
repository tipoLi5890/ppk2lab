"""Current statistics, charge, energy, peaks, and latency over sample windows.

Charge integrates rectangle-rule at the fixed 10 us sample period:
``charge_uc = sum(current_ua) * 10e-6`` (uA*s == uC).

Energy needs the DUT's supply voltage, and the PPK2 never measures it. Every
energy figure is therefore ``charge x an assumed voltage``, and the assumption
is recorded as :class:`~ppk2lab.types.VoltageBasis`:

- Source Meter mode: the DUT runs from VOUT, so the configured setpoint is a
  defensible supply voltage (still a setpoint: the DUT terminal sees slightly
  less after shunt burden and lead drop).
- Ampere Meter mode: the DUT runs from its own supply, and the device's VDD
  field is a leftover setpoint unrelated to it. Energy is reported as ``None``
  unless the caller supplies the real voltage explicitly.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from ..protocol.samples import GapEvent, SampleBlock
from ..types import SAMPLE_PERIOD_S, VoltageBasis

#: Currents above this are not physically reachable through the PPK2 shunts
#: (the top measurement range ends at 1 A); a larger value means the 4-byte
#: framing lost sync, not that the DUT drew that much.
IMPLAUSIBLE_CURRENT_UA = 1_100_000.0


@dataclass(frozen=True)
class VoltageContext:
    """The voltage used for energy, and how much that number is worth."""

    voltage_mv: int | None = None
    basis: str = VoltageBasis.UNKNOWN.value
    mode: str | None = None

    @property
    def energy_defensible(self) -> bool:
        """True when multiplying charge by this voltage yields real energy."""
        if self.voltage_mv is None:
            return False
        if self.basis == VoltageBasis.CALLER_OVERRIDE.value:
            return True
        return self.mode == "source" and self.basis in (
            VoltageBasis.CONFIGURED_SOURCE.value,
            VoltageBasis.DEVICE_METADATA.value,
        )

    def note(self) -> str | None:
        """Human-readable caveat attached to the energy figure, if any."""
        if self.voltage_mv is None:
            return "no supply voltage is known, so energy cannot be derived"
        if self.energy_defensible:
            if self.basis == VoltageBasis.CALLER_OVERRIDE.value:
                return f"energy uses the caller-supplied {self.voltage_mv} mV (not measured)"
            return (
                f"energy uses the {self.voltage_mv} mV source setpoint; the DUT terminal "
                "voltage is slightly lower (shunt burden and lead drop are not measured)"
            )
        if self.mode == "ampere":
            return (
                f"ampere mode: the device's {self.voltage_mv} mV field is a source "
                "setpoint, not the DUT's own supply, so energy is not derived. Pass the "
                "DUT's real supply voltage to compute it."
            )
        return (
            f"the {self.voltage_mv} mV reading has no recorded provenance, so it cannot "
            "be trusted as the DUT's supply. Pass the DUT's real supply voltage to "
            "compute energy."
        )


def voltage_context_from_capture(
    capture: Any, assume_voltage_mv: int | None = None
) -> VoltageContext:
    """Build the energy voltage context for a stored capture."""
    config = getattr(capture.meta, "configuration", {}) or {}
    mode = config.get("mode")
    if assume_voltage_mv is not None:
        return VoltageContext(
            voltage_mv=assume_voltage_mv,
            basis=VoltageBasis.CALLER_OVERRIDE.value,
            mode=mode,
        )
    basis = config.get("voltage_basis")
    if basis is None:
        # Artifacts written before voltage provenance was recorded still say
        # which mode they ran in, and in Source Meter mode the stored voltage
        # is the setpoint the DUT ran from. Inferring that keeps their energy
        # figures intact instead of silently voiding them.
        basis = (
            VoltageBasis.DEVICE_METADATA.value if mode == "source" else VoltageBasis.UNKNOWN.value
        )
    return VoltageContext(voltage_mv=capture.source_voltage_mv, basis=basis, mode=mode)


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
    implausible_samples: int = 0
    charge_uc: float | None = None
    energy_uj: float | None = None
    source_voltage_mv: int | None = None
    voltage_basis: str = VoltageBasis.UNKNOWN.value
    #: The PPK2 never measures the DUT terminal voltage — always False.
    voltage_measured: bool = False
    energy_note: str | None = None
    duration_s: float = 0.0
    complete: bool = True
    gaps: list[dict[str, Any]] = field(default_factory=list)

    @property
    def covered_fraction(self) -> float | None:
        """Share of the window that actually carries samples (1.0 = gap-free).

        ``None`` when an unknown-size gap makes the true span unknowable.
        """
        span = self.end_index - self.start_index
        if span <= 0:
            return None
        if self.has_unknown_gaps:
            return None
        return self.stored_samples / span

    @property
    def charge_is_lower_bound(self) -> bool:
        """True when charge/energy understate what the DUT actually drew.

        Gaps are the obvious cause, but a sample excluded for any other
        reason — an invalid range field, missing calibration, a value beyond
        what the hardware can carry — is charge that happened and was not
        counted, which makes the integral a lower bound just the same.
        """
        return bool(
            self.gap_count
            or self.invalid_range_samples
            or self.nan_samples
            or self.implausible_samples
        )

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
                "implausible": self.implausible_samples,
                "covered_fraction": self.covered_fraction,
            },
            "current_ua": {
                "mean": self.mean_ua,
                "min": self.min_ua,
                "max": self.max_ua,
                "peak_index": self.peak_index,
            },
            "charge_uc": self.charge_uc,
            "charge_is_lower_bound": self.charge_is_lower_bound,
            "energy_uj": self.energy_uj,
            "source_voltage_mv": self.source_voltage_mv,
            "voltage_basis": self.voltage_basis,
            "voltage_measured": self.voltage_measured,
            "energy_note": self.energy_note,
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
        self.implausible = 0
        # Neumaier-compensated sum. A plain running total grows without bound
        # over an hours-long capture: once it is large enough, a sub-microamp
        # sleep sample is smaller than the total's own ULP and stops
        # contributing at all — the samples that matter most for a low-power
        # measurement are exactly the ones that vanish.
        self.total = 0.0
        self._compensation = 0.0
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
            if abs(value) > IMPLAUSIBLE_CURRENT_UA:
                # Beyond what the hardware can carry: framing desync, not a
                # measurement. Counted and excluded rather than averaged in.
                self.implausible += 1
                continue
            self.valid += 1
            running = self.total + value
            if abs(self.total) >= abs(value):
                self._compensation += (self.total - running) + value
            else:
                self._compensation += (value - running) + self.total
            self.total = running
            if self.min_v is None or value < self.min_v:
                self.min_v = value
            if self.max_v is None or value > self.max_v:
                self.max_v = value
                self.peak_index = block.start_index + offset

    #: See CaptureBuilder.MAX_GAPS.
    MAX_GAPS = 10_000

    def add_gap(self, gap: GapEvent) -> None:
        if len(self.gaps) < self.MAX_GAPS:
            self.gaps.append(gap)
        self.gap_count += 1
        if gap.missing is None:
            self.unknown_gaps = True
        else:
            self.missing_known += gap.missing
            self.end_index = max(self.end_index, gap.index + gap.missing)

    @property
    def compensated_total(self) -> float:
        """The running sum with its accumulated rounding error folded back in."""
        return self.total + self._compensation

    def finalize(
        self,
        *,
        source_voltage_mv: int | None = None,
        voltage: VoltageContext | None = None,
    ) -> WindowStats:
        if voltage is None:
            voltage = VoltageContext(voltage_mv=source_voltage_mv)
        total = self.compensated_total
        charge = total * SAMPLE_PERIOD_S if self.valid else None
        energy = None
        if charge is not None and voltage.energy_defensible:
            assert voltage.voltage_mv is not None
            energy = charge * (voltage.voltage_mv / 1000.0)
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
            implausible_samples=self.implausible,
            mean_ua=(total / self.valid) if self.valid else None,
            min_ua=self.min_v,
            max_ua=self.max_v,
            peak_index=self.peak_index,
            charge_uc=charge,
            energy_uj=energy,
            source_voltage_mv=voltage.voltage_mv,
            voltage_basis=voltage.basis,
            energy_note=voltage.note(),
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
    assume_voltage_mv: int | None = None,
) -> WindowStats:
    """Statistics over a capture or a timeline window of it.

    ``start_index``/``end_index`` are timeline indexes; samples inside gaps
    simply do not exist and are counted as missing. ``assume_voltage_mv``
    supplies the DUT supply voltage the hardware cannot measure, which is what
    makes energy computable for an Ampere-mode capture.
    """
    from ..calibration import SpikeFilter

    calibration = capture.calibration
    if calibration is None:
        raise ValueError("capture has no calibration metadata; cannot compute currents")
    # The calibration correction term always uses the device's own VDD field,
    # independently of which voltage is defensible for energy.
    vdd = capture.source_voltage_mv
    voltage = voltage_context_from_capture(capture, assume_voltage_mv)
    lo = capture.start_index if start_index is None else start_index
    hi = capture.end_index if end_index is None else end_index
    acc = StatsAccumulator(lo)
    acc.end_index = hi if end_index is not None else lo
    spike = SpikeFilter() if filtered else None
    for event in capture.iter_events():
        if isinstance(event, GapEvent):
            if event.index >= hi:
                break
            if spike:
                spike.notify_gap()
            if event.missing is None:
                # Unknown-size gap: it occupies a single timeline position but
                # invalidates everything after it. A zero-width interval must
                # still count when it sits exactly on the window boundary.
                if lo <= event.index < hi or (event.index == lo == hi):
                    acc.add_gap(event)
                continue
            gap_lo, gap_hi = event.index, event.index + event.missing
            if gap_hi > lo and gap_lo < hi:
                clipped = GapEvent(
                    index=max(gap_lo, lo),
                    missing=min(gap_hi, hi) - max(gap_lo, lo),
                    reason=event.reason,
                    ambiguous=event.ambiguous,
                )
                acc.add_gap(clipped)
            continue
        block = event
        if block.start_index >= hi:
            # Events arrive in timeline order, so nothing further can fall
            # inside the window. Without this, measuring one annotation walks
            # the whole capture, and measuring N annotations walks it N times.
            break
        if block.end_index <= lo:
            continue
        s = max(0, lo - block.start_index)
        e = min(len(block), hi - block.start_index)
        sub = SampleBlock(block.start_index + s, block.words[s:e])
        currents = calibration.convert_block(sub, vdd)
        if spike:
            currents = spike.apply(currents, sub.ranges)
        acc.add_block(sub, currents)
    stats = acc.finalize(voltage=voltage)
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
