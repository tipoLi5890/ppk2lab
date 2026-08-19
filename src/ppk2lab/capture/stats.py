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

from ..diagnostics import (
    W_CLIPPED,
    W_GAP_TABLE_TRUNCATED,
    W_UNACCOUNTED_SAMPLES,
    W_WINDOW_UNPOPULATED,
    Diagnostic,
    warn,
)
from ..errors import CalibrationUnavailableError
from ..protocol.samples import ADC_FULL_SCALE, ADC_MASK, MAX_VALID_RANGE, GapEvent, SampleBlock
from ..types import SAMPLE_PERIOD_S, VoltageBasis

#: Currents above this are not physically reachable through the PPK2 shunts
#: (the top measurement range ends at 1 A); a larger value means the 4-byte
#: framing lost sync, not that the DUT drew that much.
IMPLAUSIBLE_CURRENT_UA = 1_100_000.0
#: The five auto-switching shunt ranges.
RANGE_COUNT = MAX_VALID_RANGE + 1

# --- Distribution grid -----------------------------------------------------
#
# Quantiles come from a fixed log-spaced histogram rather than from stored
# samples: the same accumulator runs live during an 8-hour capture, where
# keeping the samples would mean 2.9 billion of them. A histogram is O(1) in
# memory and exact in the only sense that matters here — the count in every
# bin is exact, and only the position inside a bin is quantized.
#
# The grid is log-spaced because the instrument is: its five shunts span
# 200 nA to 1 A, and its resolution and accuracy are both proportional
# statements, not absolute ones. A linear grid fine enough for a 6 uA sleep
# current would need 5 million bins to also reach 1 A.

#: Bottom of the grid: range 0's own resolution (200 nA). Anything at or below
#: this — including zero and the negative readings a shunt can legitimately
#: produce — lands in the underflow bin.
QUANTILE_MIN_UA = 0.2
#: Top of the grid: the top of range 4, the largest current the instrument can
#: carry at all.
QUANTILE_MAX_UA = 1_000_000.0
#: Bin density. See ``QUANTILE_HALF_WIDTH_FRACTION`` for why this number.
QUANTILE_BINS_PER_DECADE = 128
#: Bins spanning ``QUANTILE_MIN_UA`` to ``QUANTILE_MAX_UA``; a sample above
#: that lands in one of the few bins beyond it (see ``_QUANTILE_ARRAY_SIZE``).
QUANTILE_BIN_COUNT = math.ceil(
    math.log10(QUANTILE_MAX_UA / QUANTILE_MIN_UA) * QUANTILE_BINS_PER_DECADE
)
#: Worst-case relative error a reported quantile inherits from the grid:
#: +/-0.90%. Nordic's own typical accuracy is +/-10% (+/-15% on range 4), so
#: the grid contributes about a tenth of the instrument's own uncertainty and
#: is never the term that decides whether a quantile is usable.
QUANTILE_HALF_WIDTH_FRACTION = 10.0 ** (0.5 / QUANTILE_BINS_PER_DECADE) - 1.0

_QUANTILE_SCALE = QUANTILE_BINS_PER_DECADE / math.log(10.0)
#: Grid origin shifted down by one bin so the hot loop reaches the interior
#: bins (which start at index 1, after the underflow bin) without a per-sample
#: ``+ 1``. Shifting by a whole bin is exact under floor: int(x + 1) == int(x) + 1.
_QUANTILE_LOG_ORIGIN = math.log(QUANTILE_MIN_UA) - math.log(10.0) / QUANTILE_BINS_PER_DECADE
#: The grid runs past ``QUANTILE_MAX_UA`` as far as ``IMPLAUSIBLE_CURRENT_UA``,
#: which is the largest magnitude the accumulator lets through at all. That is
#: what lets the hot loop bin a sample after a single comparison: every value
#: that reaches the grid is guaranteed to land inside the array, so there is no
#: ceiling test and no chance of an index running off the end.
_QUANTILE_ARRAY_SIZE = (
    int((math.log(IMPLAUSIBLE_CURRENT_UA) - _QUANTILE_LOG_ORIGIN) * _QUANTILE_SCALE) + 1
)

# --- Typical per-range uncertainty ----------------------------------------
#
# Nordic publishes these as *typical* figures, not guaranteed limits; see
# docs/calibration.md for the tables and docs/sources.md for the references.
# They are gain specifications: systematic within a range and fully
# correlated across every sample taken in it, which is why the per-range
# contributions below are summed linearly and never in quadrature.

#: Typical accuracy per shunt range, as a fraction of reading.
RANGE_TYPICAL_ACCURACY = (0.10, 0.10, 0.10, 0.10, 0.15)
#: Typical resolution per shunt range, in uA. Used as a bound on an
#: unmeasured offset, which is why it too is summed linearly over samples.
RANGE_RESOLUTION_UA = (0.2, 0.5, 5.0, 50.0, 1000.0)
#: Identifier for the model above, so a consumer can tell whether a stored
#: figure came from these constants or from a later revision of them.
UNCERTAINTY_MODEL = "per_range_typical_v1"

#: Sub-windows the batch-means estimator aims for. Samples 10 us apart through
#: a shunt-switching front end are heavily autocorrelated, so sigma/sqrt(N)
#: over individual samples is not an estimate of anything; batch means over
#: sub-windows long compared with the correlation time are.
UNCERTAINTY_TARGET_BATCHES = 20
_MIN_BATCH_SAMPLES = 256


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


@dataclass(frozen=True)
class StatePart:
    """One side of a duty-cycle split: what the DUT did while in this state."""

    samples: int = 0
    duration_s: float = 0.0
    mean_ua: float | None = None
    charge_uc: float = 0.0
    #: Runs of consecutive *stored* samples in this state. A gap ends a run,
    #: because two samples separated by missing data are not consecutive.
    runs: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "samples": self.samples,
            "duration_s": self.duration_s,
            "mean_ua": self.mean_ua,
            "charge_uc": self.charge_uc,
            "runs": self.runs,
        }


@dataclass(frozen=True)
class StateSplit:
    """A duty-cycle decomposition of a window around one current threshold.

    The threshold travels with the split because the split is a function of
    it: "the DUT sleeps 85% of the time" is a statement about a chosen
    boundary, not about the DUT, and the same capture split at 10 uA and at
    100 uA gives two different — both correct — answers.
    """

    threshold_ua: float
    below: StatePart
    above: StatePart

    def to_json(self) -> dict[str, Any]:
        return {
            "threshold_ua": self.threshold_ua,
            "below": self.below.to_json(),
            "above": self.above.to_json(),
        }


@dataclass(frozen=True)
class TypicalUncertainty:
    """Error bars for a window: one systematic, one statistical, never mixed.

    ``*_typical`` figures come from Nordic's *typical* per-range accuracy and
    resolution (docs/calibration.md). They are not guaranteed limits and they
    are not statistical: a gain error is the same error on every sample taken
    in that range, so it does not average away and these figures do not shrink
    with capture length.

    ``mean_ua_batch_stderr`` is the separate, purely statistical question of
    how repeatable *this* mean is, estimated from sub-window batch means. It
    says nothing about whether the instrument is reading true.
    """

    mean_ua: float | None = None
    charge_uc: float | None = None
    energy_uj: float | None = None
    mean_ua_batch_stderr: float | None = None
    batch_count: int = 0
    batch_samples: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "model": UNCERTAINTY_MODEL,
            "guaranteed": False,
            "mean_ua_typical": self.mean_ua,
            "charge_uc_typical": self.charge_uc,
            "energy_uj_typical": self.energy_uj,
            "mean_ua_batch_stderr": self.mean_ua_batch_stderr,
            "batch_count": self.batch_count,
            "batch_samples": self.batch_samples,
        }


def format_with_uncertainty(value: float | None, uncertainty: float | None) -> str:
    """Render ``value +/- uncertainty`` at the precision the interval supports.

    ``1806.719544039553 +/- 188`` claims thirteen digits of a number whose
    third digit is already unknown. The uncertainty is rounded to two
    significant figures and the value to the same decimal place, which is the
    usual convention for reporting a measurement with an error bar. JSON keeps
    full precision — this is for humans only.
    """
    if value is None:
        return "n/a"
    if uncertainty is None or not math.isfinite(uncertainty) or uncertainty <= 0:
        return f"{value:g}"
    exponent = math.floor(math.log10(uncertainty)) - 1
    step = 10.0**exponent
    digits = max(0, -exponent)
    return (
        f"{round(value / step) * step:.{digits}f} +/- {round(uncertainty / step) * step:.{digits}f}"
    )


def _quantile_from_bins(
    bins: list[int],
    total: int,
    q: float,
    *,
    min_ua: float | None,
    max_ua: float | None,
) -> float | None:
    """Inverse-CDF quantile off the log grid, reported at the bin's centre.

    ``bins[0]`` holds everything at or below the grid floor — zero, negative
    readings, and anything under the instrument's own 200 nA resolution — so a
    quantile landing there is an upper bound, not an estimate. Clamping the
    result into the observed [min, max] is what makes that honest in the
    common case: a DUT whose whole sleep current sits under 200 nA reports its
    own measured minimum rather than the grid floor.
    """
    if total <= 0:
        return None
    rank = max(1, math.ceil(q * total))
    seen = 0
    for index, count in enumerate(bins):
        seen += count
        if seen >= rank:
            if index == 0:
                value = QUANTILE_MIN_UA
            else:
                # Geometric centre: the grid is log-spaced, so the midpoint of
                # a bin in log space is the value equidistant in *relative*
                # terms from both edges, which is the sense in which the
                # instrument's own accuracy is stated.
                value = QUANTILE_MIN_UA * 10.0 ** ((index - 0.5) / QUANTILE_BINS_PER_DECADE)
            if min_ua is not None and value < min_ua:
                value = min_ua
            if max_ua is not None and value > max_ua:
                value = max_ua
            return value
    return None


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
    gaps: list[dict[str, Any]] = field(default_factory=list)
    #: Gaps this capture contains but does not enumerate (see MAX_GAPS).
    gaps_truncated: int = 0
    #: Occupancy of the five auto-switching shunt ranges over the valid samples.
    samples_per_range: tuple[int, ...] = (0,) * RANGE_COUNT
    #: Charge decomposition per range; sums to ``charge_uc`` up to rounding.
    charge_per_range_uc: tuple[float, ...] | None = None
    #: Valid samples whose raw ADC field sat on full scale, per range.
    saturated_per_range: tuple[int, ...] = (0,) * RANGE_COUNT
    #: Valid samples whose raw ADC field read zero — the bottom of the range.
    zero_code_samples: int = 0
    #: Observed range changes between adjacent valid samples (never across a gap).
    range_switches: int = 0
    #: Wall-clock witness of loss the 6-bit counter cannot describe, verbatim
    #: from the capture's timeline block. Capture-level, and may be negative.
    unaccounted_samples_estimate: int | None = None
    #: True when that estimate exceeds the capture's own anchoring/clock floor.
    unaccounted_loss_is_capture_level: bool = False
    #: Distribution quantiles over the valid samples, off the log grid above.
    #: A mean answers "how much battery does this drain"; a median answers
    #: "what does this DUT actually draw", and on a duty-cycled load those are
    #: different questions with different answers.
    p50_ua: float | None = None
    p90_ua: float | None = None
    p99_ua: float | None = None
    p999_ua: float | None = None
    #: Valid samples at or below the grid floor (200 nA), where a quantile can
    #: only be reported as an upper bound.
    below_grid_samples: int = 0
    #: Valid samples reading above the instrument's top range (1 A). They are
    #: still binned, but a reading there is on the far side of the shunt the
    #: hardware was using.
    above_grid_samples: int = 0
    #: Duty-cycle decomposition, present only when a threshold was supplied.
    state_split: StateSplit | None = None
    #: Error bars; ``None`` when no sample was convertible.
    uncertainty: TypicalUncertainty | None = None

    @property
    def covered_fraction(self) -> float | None:
        """Share of the window that actually carries samples (1.0 = gap-free).

        ``None`` when an unknown-size gap makes the true span unknowable.

        This is exactly stored/span. It is deliberately not adjusted by the
        wall-clock estimate: that loss is a property of the whole capture and
        nothing localizes it to a window, so folding it in here would invent a
        localization the evidence does not support.
        """
        span = self.end_index - self.start_index
        if span <= 0:
            return None
        if self.has_unknown_gaps:
            return None
        return self.stored_samples / span

    @property
    def unpopulated_samples(self) -> int | None:
        """Window positions holding neither a stored sample nor a recorded gap.

        A window may reach past the end of the capture, or begin before its
        first sample. Those positions are not data loss — the instrument never
        reached them — but every integral over the window is still computed
        over less window than the caller asked for, and a mean over them is a
        mean over a different interval than the one requested.

        ``None`` when an unknown-size gap makes the accounting unknowable.
        """
        span = self.end_index - self.start_index
        if span <= 0:
            return 0
        if self.has_unknown_gaps:
            return None
        return max(0, span - self.stored_samples - self.missing_samples_known)

    @property
    def complete(self) -> bool:
        """True when the window is gap-free *and* populated end to end.

        "No gap event fell inside the window" is not the same statement: a
        window past the end of the capture contains no gaps because it
        contains nothing at all.
        """
        return self.gap_count == 0 and not self.unpopulated_samples

    @property
    def saturated_samples(self) -> int:
        return sum(self.saturated_per_range)

    @property
    def saturated_ranges(self) -> list[int]:
        return [r for r, n in enumerate(self.saturated_per_range) if n]

    @property
    def range_switch_rate_hz(self) -> float | None:
        if self.duration_s <= 0:
            return None
        return self.range_switches / self.duration_s

    @property
    def charge_is_lower_bound(self) -> bool:
        """True when charge/energy understate what the DUT actually drew.

        Gaps are the obvious cause, but a sample excluded for any other
        reason — an invalid range field, missing calibration, a value beyond
        what the hardware can carry — is charge that happened and was not
        counted, which makes the integral a lower bound just the same. So is a
        sample pinned at the ADC's full-scale code, which is a ceiling rather
        than a measurement; so is a stretch of window the capture never
        reached; and so is loss the wall clock witnessed but the counter could
        not describe.
        """
        return bool(
            self.gap_count
            or self.invalid_range_samples
            or self.nan_samples
            or self.implausible_samples
            or self.saturated_samples
            or self.unpopulated_samples
            or self.unaccounted_loss_is_capture_level
        )

    def diagnostics(self) -> list[Diagnostic]:
        """Coded warnings implied by these statistics.

        Lives here rather than at the call sites so the live capture path and
        the offline ``measure`` path cannot disagree about what a number means.
        """
        out: list[Diagnostic] = []
        unpopulated = self.unpopulated_samples
        if unpopulated:
            out.append(
                warn(
                    W_WINDOW_UNPOPULATED,
                    f"{unpopulated:,} of the {self.end_index - self.start_index:,} requested "
                    "window positions hold no data: the window reaches past what the capture "
                    "covers. Statistics describe the populated part only, and the charge is a "
                    "lower bound on the window as asked for",
                )
            )
        if self.gaps_truncated:
            out.append(
                warn(
                    W_GAP_TABLE_TRUNCATED,
                    f"{self.gaps_truncated:,} gap(s) are counted but not enumerated; the gap "
                    "table hit its ceiling, so individual missing spans cannot all be located",
                )
            )
        if self.saturated_samples:
            detail = ", ".join(
                f"range {r}: {n:,}" for r, n in enumerate(self.saturated_per_range) if n
            )
            top = MAX_VALID_RANGE in self.saturated_ranges
            cause = (
                "the DUT drew more than the instrument's top range can carry"
                if top
                else "the auto-range switch had not completed"
            )
            out.append(
                warn(
                    W_CLIPPED,
                    f"{self.saturated_samples:,} sample(s) sat on the ADC's full-scale code "
                    f"({detail}): {cause}, so those amplitudes are understated and every "
                    "integral over this window is a lower bound",
                )
            )
        if self.unaccounted_loss_is_capture_level:
            out.append(
                warn(
                    W_UNACCOUNTED_SAMPLES,
                    f"the capture's wall clock accounts for about "
                    f"{self.unaccounted_samples_estimate:,} more samples than its timeline "
                    "does. That loss is real but capture-level: nothing places it inside any "
                    "window, so coverage cannot show it and integrals are lower bounds",
                )
            )
        return out

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
                "unpopulated": self.unpopulated_samples,
                "saturated": self.saturated_samples,
                "zero_code": self.zero_code_samples,
                "covered_fraction": self.covered_fraction,
            },
            "current_ua": {
                "mean": self.mean_ua,
                "min": self.min_ua,
                "max": self.max_ua,
                "peak_index": self.peak_index,
                "p50": self.p50_ua,
                "p90": self.p90_ua,
                "p99": self.p99_ua,
                "p999": self.p999_ua,
            },
            "distribution": {
                "grid_min_ua": QUANTILE_MIN_UA,
                "grid_max_ua": QUANTILE_MAX_UA,
                "bins_per_decade": QUANTILE_BINS_PER_DECADE,
                "quantile_half_width_fraction": QUANTILE_HALF_WIDTH_FRACTION,
                "below_grid_samples": self.below_grid_samples,
                "above_grid_samples": self.above_grid_samples,
            },
            "state_split": None if self.state_split is None else self.state_split.to_json(),
            "uncertainty": None if self.uncertainty is None else self.uncertainty.to_json(),
            "charge_uc": self.charge_uc,
            "charge_is_lower_bound": self.charge_is_lower_bound,
            "energy_uj": self.energy_uj,
            "source_voltage_mv": self.source_voltage_mv,
            "voltage_basis": self.voltage_basis,
            "voltage_measured": self.voltage_measured,
            "energy_note": self.energy_note,
            "complete": self.complete,
            "saturated_samples": self.saturated_samples,
            "saturated_ranges": self.saturated_ranges,
            "samples_per_range": list(self.samples_per_range),
            "charge_per_range_uc": (
                None if self.charge_per_range_uc is None else list(self.charge_per_range_uc)
            ),
            "range_switches": self.range_switches,
            "range_switch_rate_hz": self.range_switch_rate_hz,
            "unaccounted_samples_estimate": self.unaccounted_samples_estimate,
            "unaccounted_loss_is_capture_level": self.unaccounted_loss_is_capture_level,
            "sample_gaps": self.gaps,
            "gaps_truncated": self.gaps_truncated,
        }


class StatsAccumulator:
    """Online statistics over a stream of calibrated blocks."""

    def __init__(self, start_index: int, *, state_threshold_ua: float | None = None) -> None:
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
        #: Gaps this capture holds but never enumerated, carried in from the
        #: source (a stored capture arrives with its table already truncated).
        self.gaps_truncated = 0
        # Per-range occupancy. Plain sums: each is a partition of the total,
        # and a double holds an hours-long per-range sum without the smallest
        # sample falling below its ULP. The authoritative charge stays the
        # compensated total.
        self.samples_per_range = [0] * RANGE_COUNT
        self.total_per_range = [0.0] * RANGE_COUNT
        self.saturated_per_range = [0] * RANGE_COUNT
        self.zero_code = 0
        self.range_switches = 0
        self._last_range: int | None = None
        # Log-spaced distribution grid: bin 0 is everything at or below the
        # grid floor and bins 1.. are the log grid itself. Fixed size, so an
        # 8-hour capture costs exactly what a 50 ms one does.
        self.bins = [0] * _QUANTILE_ARRAY_SIZE
        # Duty-cycle split. ``None`` means no threshold was asked for, and the
        # loop then skips the accounting entirely and reports no split — a
        # split with no threshold beside it cannot be read. This is the one
        # statistic here that depends on a caller's choice; everything else is
        # collected unconditionally so the live and offline paths cannot
        # disagree about the same window.
        self.state_threshold_ua = state_threshold_ua
        self._state_n = [0, 0]
        self._state_total = [0.0, 0.0]
        self._state_runs = [0, 0]
        self._state_was = -1
        # Sub-window batches for the statistical error bar. Closed on the
        # valid-sample count, so the batching is a property of the window and
        # not of how the samples were delivered.
        self._batches: list[tuple[float, int]] = []
        self._batch_size = _MIN_BATCH_SAMPLES
        self._batch_sum = 0.0
        self._next_flush = _MIN_BATCH_SAMPLES

    def add_block(self, block: SampleBlock, currents_ua: list[float]) -> None:
        """Fold one calibrated block into every running statistic.

        This is the only loop that touches every sample, so range occupancy,
        saturation, switch counting and the distribution grid are fused into it
        rather than paid for in a second pass. Measured on the author's machine
        against a verbatim copy of the previous loop, interleaved, min of 25
        runs over 300,000 samples of the demo profile: 121.4 ns/sample before
        the distribution work, 172.9 with it (+42%), and 190.0 (+56%) when a
        duty-cycle threshold is also being tracked. That is 1.7% of one core at
        real time and about 62 s of CPU for an hour-long capture measured
        offline. It is not free, and the grid is not optional: a mean alone
        cannot say what a duty-cycled DUT draws, and there is nowhere else to
        collect the distribution without keeping every sample.

        Every running total is held in a local and written back once: the loop
        body is executed 100,000 times per second of capture, and an attribute
        store per sample is the difference between the figures above and ones
        roughly three times larger.
        """
        self.stored += len(block)
        self.end_index = max(self.end_index, block.end_index)
        ranges = block.ranges
        per_range = self.samples_per_range
        # Range occupancy comes from a C-level scan of the range bytes rather
        # than an increment per sample; the loop only has to subtract the few
        # samples it goes on to exclude. Invalid range fields are above
        # MAX_VALID_RANGE and so are never counted here in the first place.
        for range_index in range(RANGE_COUNT):
            per_range[range_index] += ranges.count(range_index)
        charge_per_range = self.total_per_range
        saturated = self.saturated_per_range
        last_range = self._last_range
        switches = self.range_switches
        zero_code = self.zero_code
        valid = self.valid
        invalid_range = self.invalid_range
        nans = self.nans
        implausible = self.implausible
        total = self.total
        compensation = self._compensation
        min_v = self.min_v
        max_v = self.max_v
        peak_index = self.peak_index
        start_index = block.start_index
        isnan = math.isnan
        offset = -1
        bins = self.bins
        log = math.log
        grid_min = QUANTILE_MIN_UA
        grid_origin = _QUANTILE_LOG_ORIGIN
        grid_scale = _QUANTILE_SCALE
        configured = self.state_threshold_ua
        track_state = configured is not None
        threshold = math.inf if configured is None else configured
        below_n, above_n = self._state_n
        below_total, above_total = self._state_total
        below_runs, above_runs = self._state_runs
        state_was = self._state_was
        batches = self._batches
        batch_size = self._batch_size
        batch_sum = self._batch_sum
        next_flush = self._next_flush
        merge_at = 2 * UNCERTAINTY_TARGET_BATCHES
        # The ADC field is read straight from the raw words rather than through
        # block.adc: that property materializes a whole extra list per block,
        # which is the allocation this accumulator exists to avoid.
        for value, range_index, word in zip(currents_ua, ranges, block.words, strict=True):
            offset += 1
            if range_index > MAX_VALID_RANGE:
                invalid_range += 1
                continue
            if isnan(value):
                nans += 1
                per_range[range_index] -= 1
                continue
            if value > IMPLAUSIBLE_CURRENT_UA or value < -IMPLAUSIBLE_CURRENT_UA:
                # Beyond what the hardware can carry: framing desync, not a
                # measurement. Counted and excluded rather than averaged in.
                implausible += 1
                per_range[range_index] -= 1
                continue
            valid += 1
            if range_index != last_range:
                if last_range is not None:
                    switches += 1
                last_range = range_index
            charge_per_range[range_index] += value
            adc = word & ADC_MASK
            if not 0 < adc < ADC_FULL_SCALE:
                if adc:
                    # Pinned, not measured: the calibrated value is a ceiling
                    # and no arithmetic on it recovers the real current.
                    saturated[range_index] += 1
                else:
                    zero_code += 1
            # The grid is entered with a natural log and a scale factor rather
            # than log10: same bins, one multiply instead of a slower C call.
            if value > grid_min:
                bins[int((log(value) - grid_origin) * grid_scale)] += 1
            else:
                # Zero, negative, or under the instrument's own resolution.
                # log() is undefined for the first two, and the third cannot be
                # placed on a grid that starts at 200 nA.
                bins[0] += 1
            if track_state:
                if value >= threshold:
                    above_n += 1
                    above_total += value
                    if state_was != 1:
                        above_runs += 1
                        state_was = 1
                else:
                    below_n += 1
                    below_total += value
                    if state_was != 0:
                        below_runs += 1
                        state_was = 0
            # Sub-window batch for the statistical error bar. Batched on the
            # valid-sample count rather than on the per-block sums the loop
            # already produces, because block boundaries are a property of how
            # the samples arrived: the live stream delivers small USB blocks
            # and a stored capture delivers whole chunks, so batching on those
            # would make a capture's own recorded stderr disagree with a later
            # `measure` of the same window.
            batch_sum += value
            if valid == next_flush:
                batches.append((batch_sum, batch_size))
                batch_sum = 0.0
                if len(batches) == merge_at:
                    # The capture length is never known in advance, so batches
                    # are grown by halving the list: the count stays between
                    # the target and twice it, and merging sums is exact.
                    batches[:] = [
                        (batches[i][0] + batches[i + 1][0], batches[i][1] + batches[i + 1][1])
                        for i in range(0, merge_at, 2)
                    ]
                    batch_size += batch_size
                next_flush = valid + batch_size
            running = total + value
            if abs(total) >= abs(value):
                compensation += (total - running) + value
            else:
                compensation += (value - running) + total
            total = running
            if min_v is None or value < min_v:
                min_v = value
            if max_v is None or value > max_v:
                max_v = value
                peak_index = start_index + offset
        self._last_range = last_range
        self.range_switches = switches
        self.zero_code = zero_code
        self.valid = valid
        self.invalid_range = invalid_range
        self.nans = nans
        self.implausible = implausible
        self.total = total
        self._compensation = compensation
        self.min_v = min_v
        self.max_v = max_v
        self.peak_index = peak_index
        self._state_n = [below_n, above_n]
        self._state_total = [below_total, above_total]
        self._state_runs = [below_runs, above_runs]
        self._state_was = state_was
        self._batch_size = batch_size
        self._batch_sum = batch_sum
        self._next_flush = next_flush

    def _batch_stderr(self) -> tuple[float | None, int, int]:
        """Standard error of the mean from batch means, not from sigma/sqrt(N).

        Consecutive 10 us samples through a shunt-switching front end are not
        independent draws: a duty-cycled load holds one level for thousands of
        them. Dividing the per-sample standard deviation by sqrt(N) would
        therefore report an error bar that shrinks with capture length whether
        or not anything settled. Batch means over sub-windows long compared
        with the correlation time do not have that defect.
        """
        batches = list(self._batches)
        pending = self.valid - sum(n for _, n in batches)
        if pending:
            # Fold a short tail into the last batch rather than letting a
            # sliver of a window contribute a high-variance batch mean.
            if batches and pending * 2 < self._batch_size:
                last_sum, last_n = batches[-1]
                batches[-1] = (last_sum + self._batch_sum, last_n + pending)
            else:
                batches.append((self._batch_sum, pending))
        count = len(batches)
        samples = sum(n for _, n in batches)
        if count < 2:
            return None, count, samples
        means = [s / n for s, n in batches]
        grand = sum(means) / count
        variance = sum((m - grand) ** 2 for m in means) / (count - 1)
        return math.sqrt(variance / count), count, samples // count

    #: See CaptureBuilder.MAX_GAPS.
    MAX_GAPS = 10_000

    def add_gap(self, gap: GapEvent) -> None:
        if len(self.gaps) < self.MAX_GAPS:
            self.gaps.append(gap)
        else:
            self.gaps_truncated += 1
        self.gap_count += 1
        # Two samples separated by missing data are not adjacent, so a range
        # difference across a gap is not an observed switch, and a run of
        # above-threshold samples that resumes after a gap is a second
        # observed run rather than a continuation of the first.
        self._last_range = None
        self._state_was = -1
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
        timing: dict[str, Any] | None = None,
    ) -> WindowStats:
        if voltage is None:
            voltage = VoltageContext(voltage_mv=source_voltage_mv)
        total = self.compensated_total
        charge = total * SAMPLE_PERIOD_S if self.valid else None
        energy = None
        if charge is not None and voltage.energy_defensible:
            assert voltage.voltage_mv is not None
            energy = charge * (voltage.voltage_mv / 1000.0)
        estimate, above_floor = _unaccounted(timing)
        charge_per_range = (
            tuple(v * SAMPLE_PERIOD_S for v in self.total_per_range) if self.valid else None
        )
        stderr, batch_count, batch_samples = self._batch_stderr()
        uncertainty = _typical_uncertainty(
            charge_per_range=charge_per_range,
            samples_per_range=self.samples_per_range,
            valid=self.valid,
            energy_uj=energy,
            stderr=stderr,
            batch_count=batch_count,
            batch_samples=batch_samples,
        )
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
            gaps=[g.to_json() for g in self.gaps],
            gaps_truncated=self.gaps_truncated,
            samples_per_range=tuple(self.samples_per_range),
            charge_per_range_uc=charge_per_range,
            saturated_per_range=tuple(self.saturated_per_range),
            zero_code_samples=self.zero_code,
            range_switches=self.range_switches,
            unaccounted_samples_estimate=estimate,
            unaccounted_loss_is_capture_level=above_floor,
            p50_ua=self.quantile(0.50),
            p90_ua=self.quantile(0.90),
            p99_ua=self.quantile(0.99),
            p999_ua=self.quantile(0.999),
            below_grid_samples=self.bins[0],
            above_grid_samples=sum(self.bins[QUANTILE_BIN_COUNT + 1 :]),
            state_split=self.state_split(),
            uncertainty=uncertainty,
        )

    def quantile(self, q: float) -> float | None:
        """The ``q``-quantile of the valid samples, off the log grid.

        ``None`` when nothing convertible was seen. The value is quantized to
        the grid, so it carries ``QUANTILE_HALF_WIDTH_FRACTION`` of relative
        error on top of whatever the instrument itself contributes.
        """
        return _quantile_from_bins(self.bins, self.valid, q, min_ua=self.min_v, max_ua=self.max_v)

    def state_split(self) -> StateSplit | None:
        """The duty-cycle decomposition, or ``None`` if no threshold was set."""
        threshold = self.state_threshold_ua
        if threshold is None:
            return None
        parts = []
        for state in (0, 1):
            n = self._state_n[state]
            total = self._state_total[state]
            parts.append(
                StatePart(
                    samples=n,
                    duration_s=n * SAMPLE_PERIOD_S,
                    mean_ua=(total / n) if n else None,
                    charge_uc=total * SAMPLE_PERIOD_S,
                    runs=self._state_runs[state],
                )
            )
        return StateSplit(threshold_ua=threshold, below=parts[0], above=parts[1])


def _typical_uncertainty(
    *,
    charge_per_range: tuple[float, ...] | None,
    samples_per_range: list[int],
    valid: int,
    energy_uj: float | None,
    stderr: float | None,
    batch_count: int,
    batch_samples: int,
) -> TypicalUncertainty | None:
    """Typical per-range error bars for a window's charge, mean, and energy.

    The model is one line per range::

        charge_uncertainty = sum_r ( accuracy_r * |charge_r| + resolution_r * n_r * T )

    Two properties of that line are load-bearing and easy to get wrong.

    *The contributions are summed linearly.* Nordic's per-range accuracy is a
    gain specification: within one range it is the same error on every sample,
    so it is fully correlated and does not partially cancel. Combining the
    ranges in quadrature — or dividing by sqrt(N) — would report an error bar
    that shrinks with capture length, which a systematic error does not do.

    *The resolution term is separate and necessary.* A purely multiplicative
    model says a 6 mA reading in range 3 is uncertain by 600 uA and a 5.1 mA
    reading by 510 uA, while both sit on a grid whose step is 50 uA; near the
    bottom of a range that step is the dominant term, and a model without it
    understates the error exactly where a low-power measurement lives. It is
    summed linearly for the same reason as the gain term: an unmeasured offset
    of up to one resolution step is the same offset on every sample.

    Returns ``None`` when no sample was convertible — an error bar on nothing
    is not a smaller error bar.
    """
    if charge_per_range is None or valid <= 0:
        return None
    charge_uncertainty = 0.0
    for index, charge in enumerate(charge_per_range):
        charge_uncertainty += RANGE_TYPICAL_ACCURACY[index] * abs(charge)
        charge_uncertainty += (
            RANGE_RESOLUTION_UA[index] * samples_per_range[index] * SAMPLE_PERIOD_S
        )
    mean_uncertainty = charge_uncertainty / (valid * SAMPLE_PERIOD_S)
    total_charge = sum(charge_per_range)
    energy_uncertainty = None
    if energy_uj is not None and total_charge:
        # Only the current side. The supply voltage is an assumption the PPK2
        # never measures (docs/energy-analysis.md), so it has no error bar to
        # propagate and this figure must not be read as one.
        energy_uncertainty = abs(energy_uj) * (charge_uncertainty / abs(total_charge))
    return TypicalUncertainty(
        mean_ua=mean_uncertainty,
        charge_uc=charge_uncertainty,
        energy_uj=energy_uncertainty,
        mean_ua_batch_stderr=stderr,
        batch_count=batch_count,
        batch_samples=batch_samples,
    )


def _unaccounted(timing: dict[str, Any] | None) -> tuple[int | None, bool]:
    """Read the wall-clock loss witness out of a capture's timeline block.

    The estimate is reported verbatim, including when it is negative or inside
    the noise: the flag, not the number, is what says the loss is real. See
    ``capture/runner.timeline_report`` for how both are derived.
    """
    if not timing:
        return None, False
    estimate = timing.get("unaccounted_samples_estimate")
    floor = timing.get("unaccounted_floor_samples")
    if not isinstance(estimate, int) or isinstance(estimate, bool):
        return None, False
    above = isinstance(floor, int) and not isinstance(floor, bool) and estimate > floor
    return estimate, above


def compute_stats(
    capture: Any,
    *,
    start_index: int | None = None,
    end_index: int | None = None,
    filtered: bool = False,
    assume_voltage_mv: int | None = None,
    state_threshold_ua: float | None = None,
) -> WindowStats:
    """Statistics over a capture or a timeline window of it.

    ``start_index``/``end_index`` are timeline indexes; samples inside gaps
    simply do not exist and are counted as missing. ``assume_voltage_mv``
    supplies the DUT supply voltage the hardware cannot measure, which is what
    makes energy computable for an Ampere-mode capture.

    ``state_threshold_ua`` adds a duty-cycle decomposition around that current.
    It is the one statistic here that depends on a caller's choice, so it is
    opt-in and the threshold is reported back inside the result; everything
    else — quantiles included — is collected unconditionally, so measuring a
    window offline can never disagree with what the same window recorded live.
    """
    from ..calibration import SpikeFilter

    calibration = capture.calibration
    if calibration is None:
        raise CalibrationUnavailableError(
            "capture has no calibration metadata; cannot compute currents"
        )
    # The calibration correction term always uses the device's own VDD field,
    # independently of which voltage is defensible for energy.
    vdd = capture.source_voltage_mv
    voltage = voltage_context_from_capture(capture, assume_voltage_mv)
    lo = capture.start_index if start_index is None else start_index
    hi = capture.end_index if end_index is None else end_index
    acc = StatsAccumulator(lo, state_threshold_ua=state_threshold_ua)
    acc.end_index = hi if end_index is not None else lo
    # Gaps the capture never enumerated are invisible to this walk, so they
    # have to be carried in rather than counted.
    acc.gaps_truncated = int(getattr(capture, "gaps_truncated", 0) or 0)
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
    stats = acc.finalize(voltage=voltage, timing=getattr(capture, "timing", None))
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
        raise CalibrationUnavailableError("capture has no calibration metadata")
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
