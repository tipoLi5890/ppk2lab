"""Compare one capture against another, and say what the difference is worth.

A power question is usually a difference: what does enabling this rail cost,
what did this firmware change, how much does that LED draw. Answering it by
quoting two absolute numbers from a +/-10% instrument throws away the reason
the paired measurement was made in the first place.

The +/-10% in ``RANGE_TYPICAL_ACCURACY`` is a *gain* error: a fraction of
reading, the same fraction for every sample taken through that shunt (see
``TypicalUncertainty``, which says so explicitly — it does not shrink with
capture length because it is not statistical). Two captures taken minutes
apart through the same shunt share that unknown factor k, so

    delta_measured = k * a_true - k * b_true = k * delta_true

and the gain error scales the *difference*, contributing
``accuracy * |delta|`` rather than ``accuracy * (|a| + |b|)``. On a 54 uA
difference between two ~200 uA readings that is the difference between an
error bar of +/-6 uA and one of +/-45 uA — between a measurement and a shrug.

The cancellation is a claim about the shunt, not about arithmetic, so it is
made only when both captures really did stay in one and the same range. When
they did not, the two gains are independent unknowns and the terms add. The
resolution term is always added: it bounds an offset per sample rather than
scaling a reading, and claiming it cancels would be claiming more than the
model supports.
"""

from __future__ import annotations

from typing import Any

from ..capture.model import Capture
from ..capture.stats import (
    RANGE_RESOLUTION_UA,
    RANGE_TYPICAL_ACCURACY,
    UNCERTAINTY_MODEL,
    WindowStats,
)
from ..errors import UsageError
from ..types import SAMPLE_PERIOD_S

#: Share of both a capture's valid samples *and* its absolute charge that must
#: sit in one range before that range is called the capture's own. Below this
#: the capture switched ranges during the measurement and no single gain factor
#: describes it.
#:
#: Both measures are required because the metrics being differenced are
#: charge-weighted while a sample count is not. A duty-cycled load puts 99.6% of
#: its *samples* in the microamp range and 99.8% of its *charge* in the
#: milliamp one; calling that a single-range capture prices the delta with the
#: wrong shunt's accuracy and publishes a sentence about the hardware that is
#: not true.
DOMINANT_RANGE_SHARE = 0.995

#: Metrics whose difference this model can price. The others are still
#: differenced — they are just reported without an error bar rather than with
#: an invented one.
_MODELLED = {"mean_ua", "charge_uc"}

_METRIC_FIELDS: dict[str, tuple[str, str]] = {
    "mean_current": ("mean_ua", "uA"),
    "avg_current": ("mean_ua", "uA"),
    "max_current": ("max_ua", "uA"),
    "peak_current": ("max_ua", "uA"),
    "min_current": ("min_ua", "uA"),
    "p5_current": ("p5_ua", "uA"),
    "p50_current": ("p50_ua", "uA"),
    "median_current": ("p50_ua", "uA"),
    "p90_current": ("p90_ua", "uA"),
    "p95_current": ("p95_ua", "uA"),
    "p99_current": ("p99_ua", "uA"),
    "p999_current": ("p999_ua", "uA"),
    "charge": ("charge_uc", "uC"),
    "energy": ("energy_uj", "uJ"),
}


def metric_names() -> list[str]:
    """Metric names ``compare`` accepts, in a stable order."""
    return sorted(_METRIC_FIELDS)


def dominant_range(stats: WindowStats) -> int | None:
    """The single shunt range a capture stayed in, or None if it moved.

    Dominance is required in samples *and* in absolute charge; see
    :data:`DOMINANT_RANGE_SHARE`. Without a charge breakdown there is no way to
    check the second, so no cancellation claim is made.
    """
    per_range = stats.samples_per_range
    charge = stats.charge_per_range_uc
    if not per_range or not charge:
        return None
    total = sum(per_range)
    charge_total = sum(abs(c) for c in charge)
    if total <= 0 or charge_total <= 0:
        return None
    best = max(range(len(per_range)), key=lambda i: per_range[i])
    if per_range[best] / total < DOMINANT_RANGE_SHARE:
        return None
    if best >= len(charge) or abs(charge[best]) / charge_total < DOMINANT_RANGE_SHARE:
        return None
    return best


def _gain_and_resolution(stats: WindowStats) -> tuple[float, float]:
    """Split a window's mean-current error bar into its two terms, in uA."""
    charge_per_range: list[float] = list(stats.charge_per_range_uc or [])
    per_range: list[int] = list(stats.samples_per_range or [])
    valid = stats.valid_samples
    if not charge_per_range or valid <= 0:
        return (0.0, 0.0)
    gain_charge = sum(
        RANGE_TYPICAL_ACCURACY[i] * abs(charge) for i, charge in enumerate(charge_per_range)
    )
    resolution_charge = sum(
        RANGE_RESOLUTION_UA[i] * per_range[i] * SAMPLE_PERIOD_S for i in range(len(per_range))
    )
    denominator = valid * SAMPLE_PERIOD_S
    return (gain_charge / denominator, resolution_charge / denominator)


def compare_stats(
    a: WindowStats,
    b: WindowStats,
    *,
    metric: str = "mean_current",
    same_instrument: bool | None = None,
) -> dict[str, Any]:
    """Difference ``b - a`` for one metric, with an error bar where the model
    supports one.

    ``b - a`` reads as the change from the first capture to the second, which
    is the order the arguments are given in. ``relative`` divides by
    ``abs(a_value)``, so its sign always matches the delta's — this instrument
    legitimately reads below zero on an unloaded input, and a signed
    denominator turned a rise against a negative baseline into a reported fall.

    ``same_instrument`` is the shunt half of the cancellation premise: *k* is
    one physical unit's residual gain error, and two units have independent
    *k*. ``False`` suppresses the cancellation even when both captures stayed
    in the same range; ``None`` means the instruments were not identified and
    the claim is made but flagged as unverified.
    """
    if metric not in _METRIC_FIELDS:
        raise UsageError(
            f"unknown metric {metric!r}",
            remediation=f"Use one of: {', '.join(metric_names())}.",
        )
    field, unit = _METRIC_FIELDS[metric]
    a_value = getattr(a, field, None)
    b_value = getattr(b, field, None)

    range_a = dominant_range(a)
    range_b = dominant_range(b)
    if range_a is None or range_b is None:
        basis = "mixed"
    elif range_a == range_b:
        basis = "same_range"
    else:
        basis = "cross_range"

    delta = None if a_value is None or b_value is None else b_value - a_value
    result: dict[str, Any] = {
        "metric": metric,
        "unit": unit,
        "a_value": a_value,
        "b_value": b_value,
        "delta": delta,
        # abs(): see the docstring. A negative baseline is an ordinary state of
        # this instrument, not an error case.
        "relative": (delta / abs(a_value) if delta is not None and a_value else None),
        "basis": basis,
        "dominant_range": {"a": range_a, "b": range_b},
        "same_instrument": same_instrument,
        "uncertainty": None,
    }
    if field == "energy_uj":
        # energy is charge x V, and V comes from each capture's own supply.
        # Differencing two captures taken at different setpoints reports the
        # instrument's configuration as if it were the DUT's behaviour.
        result["voltage"] = {
            "a": a.source_voltage_mv,
            "b": b.source_voltage_mv,
            "basis": {"a": a.voltage_basis, "b": b.voltage_basis},
            "differs": (
                a.source_voltage_mv is not None
                and b.source_voltage_mv is not None
                and a.source_voltage_mv != b.source_voltage_mv
            ),
            "note": {"a": a.energy_note, "b": b.energy_note},
        }
    if field not in _MODELLED or delta is None:
        result["uncertainty_note"] = (
            f"no error bar is modelled for {metric}; the difference is reported as measured"
        )
        return result

    gain_a, res_a = _gain_and_resolution(a)
    gain_b, res_b = _gain_and_resolution(b)
    if field == "charge_uc":
        # The stored terms are per-sample means; scale each back to its own
        # window's charge before differencing them.
        gain_a *= a.valid_samples * SAMPLE_PERIOD_S
        gain_b *= b.valid_samples * SAMPLE_PERIOD_S
        res_a *= a.valid_samples * SAMPLE_PERIOD_S
        res_b *= b.valid_samples * SAMPLE_PERIOD_S

    if basis == "same_range" and same_instrument is False and range_a is not None:
        # Same shunt *index* on two different units is not the same shunt. The
        # basis stays "same_range" because that is a true statement about the
        # ranges; only the cancellation is withdrawn.
        gain_term = gain_a + gain_b
        note = (
            f"both captures stayed in range {range_a}, but they came from different "
            "instruments, so their gain errors are independent unknowns and add"
        )
    elif basis == "same_range" and range_a is not None:
        gain_term = RANGE_TYPICAL_ACCURACY[range_a] * abs(delta)
        note = (
            f"both captures stayed in range {range_a}, so the gain error is one shared "
            "unknown and scales the difference rather than each reading"
        )
        if same_instrument is None:
            note += (
                "; the instruments were not identified, so the shared-shunt premise is unverified"
            )
    else:
        gain_term = gain_a + gain_b
        note = (
            "the captures did not share a single shunt range, so their gain errors are "
            "independent and add; this bar is not tighter than the two absolute ones"
        )
    resolution_term = res_a + res_b

    se_a = a.uncertainty.mean_ua_batch_stderr if a.uncertainty else None
    se_b = b.uncertainty.mean_ua_batch_stderr if b.uncertainty else None
    stderr = None
    if field == "mean_ua" and se_a is not None and se_b is not None:
        stderr = (se_a**2 + se_b**2) ** 0.5

    result["uncertainty"] = {
        "model": UNCERTAINTY_MODEL,
        "guaranteed": False,
        "delta_typical": gain_term + resolution_term,
        "gain_term": gain_term,
        "resolution_term": resolution_term,
        "gain_error_cancels": basis == "same_range" and same_instrument is not False,
        "delta_batch_stderr": stderr,
        "note": note,
    }
    return result


def summarize_side(capture: Capture, stats: WindowStats) -> dict[str, Any]:
    """What a reader needs to judge whether a side is worth comparing.

    The voltage block is read off ``stats``, not off
    ``capture.meta.configuration``: with ``--assume-voltage-mv`` the recorded
    setpoint is not what the energy figure was computed from, and reporting the
    setpoint would describe a number nobody produced.
    """
    device = capture.meta.device or {}
    return {
        "capture_id": capture.meta.capture_id,
        "created_utc": capture.meta.created_utc,
        "user_tags": dict(capture.meta.user_tags),
        "device": {
            "serial_number": device.get("serial_number"),
            "firmware_version": device.get("firmware_version"),
        },
        "complete": capture.complete,
        "covered_fraction": stats.covered_fraction,
        "duration_s": stats.duration_s,
        "gap_count": stats.gap_count,
        # Evidence that an operand is a bound rather than a measurement. A
        # delta between two floor-served quantiles is zero for a reason that
        # has nothing to do with the DUT.
        "quantiles_at_floor": list(stats.quantiles_at_floor),
        "saturated_samples": stats.saturated_samples,
        "charge_is_lower_bound": stats.charge_is_lower_bound,
        "source_voltage_mv": stats.source_voltage_mv,
        "voltage_basis": stats.voltage_basis,
        "energy_note": stats.energy_note,
    }
