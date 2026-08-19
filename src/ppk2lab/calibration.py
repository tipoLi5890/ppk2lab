"""Calibrated current conversion.

For measurement range ``r`` with device calibration constants (see
docs/calibration.md):

.. code-block:: text

    adc = ADC_field * 4
    x = (adc - O[r]) * ((1.8 / 163840) / R[r])          # amperes: volts / ohms
    current_A  = UG[r] * ( x * (GS[r] * x + GI[r]) + S[r] * (VDD_mV / 1000) + I[r] )
    current_uA = current_A * 1e6

The expression is dimensionally amperes (the ``R`` constants are real shunt
resistances in ohms — verified against device metadata on hardware, e.g.
R0 ≈ 1000.6 Ω); the API reports microamperes. Samples in a range with
missing or NaN constants convert to ``NaN`` — unknown calibration is
preserved, never replaced by invented defaults.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .protocol.metadata import Metadata
from .protocol.samples import ADC_MULTIPLIER, MAX_VALID_RANGE, SampleBlock

ADC_REFERENCE_FACTOR = 1.8 / 163840
#: The calibration expression yields amperes; the public API reports uA.
MICROAMP_PER_AMP = 1e6


@dataclass(frozen=True)
class RangeCalibration:
    r: float
    gs: float
    gi: float
    o: float
    s: float
    i: float
    ug: float

    def convert(self, adc_field: int, vdd_mv: float) -> float:
        adc = adc_field * ADC_MULTIPLIER
        x = (adc - self.o) * (ADC_REFERENCE_FACTOR / self.r)
        amps = self.ug * (x * (self.gs * x + self.gi) + self.s * (vdd_mv / 1000.0) + self.i)
        return amps * MICROAMP_PER_AMP


class Calibration:
    """Per-range calibrated conversion built from parsed device metadata."""

    def __init__(
        self,
        ranges: list[RangeCalibration | None],
        *,
        vdd_mv: int | None = None,
        calibrated: bool | None = None,
    ) -> None:
        if len(ranges) != 5:
            raise ValueError("exactly 5 measurement ranges expected")
        self.ranges = ranges
        self.vdd_mv = vdd_mv
        self.calibrated = calibrated

    @classmethod
    def from_metadata(cls, metadata: Metadata) -> Calibration:
        ranges: list[RangeCalibration | None] = []
        for idx in range(5):
            values = {}
            usable = True
            for family, attr in (
                ("R", "r"),
                ("GS", "gs"),
                ("GI", "gi"),
                ("O", "o"),
                ("S", "s"),
                ("I", "i"),
                ("UG", "ug"),
            ):
                value = metadata.cal[family][idx]
                if value is None or math.isnan(value):
                    usable = False
                    break
                values[attr] = value
            if usable and values["r"] != 0:
                ranges.append(RangeCalibration(**values))
            else:
                ranges.append(None)
        return cls(ranges, vdd_mv=metadata.vdd_mv, calibrated=metadata.calibrated)

    def missing_ranges(self) -> list[int]:
        return [i for i, r in enumerate(self.ranges) if r is None]

    def convert(self, adc_field: int, range_index: int, vdd_mv: float | None = None) -> float:
        """Convert one raw ADC field to microamps; NaN when not convertible."""
        if range_index > MAX_VALID_RANGE:
            return math.nan
        cal = self.ranges[range_index]
        if cal is None:
            return math.nan
        vdd = vdd_mv if vdd_mv is not None else self.vdd_mv
        if vdd is None:
            return math.nan
        return cal.convert(adc_field, vdd)

    def convert_block(self, block: SampleBlock, vdd_mv: float | None = None) -> list[float]:
        """Vector conversion of a sample block to microamps (NaN preserved)."""
        vdd = vdd_mv if vdd_mv is not None else self.vdd_mv
        adcs = block.adc
        ranges = block.ranges
        out: list[float] = []
        cal_table = self.ranges
        for adc_field, r in zip(adcs, ranges, strict=True):
            cal = cal_table[r] if r <= MAX_VALID_RANGE else None
            if cal is None or vdd is None:
                out.append(math.nan)
            else:
                out.append(cal.convert(adc_field, vdd))
        return out


class SpikeFilter:
    """Optional smoothing of range-switch transients.

    When the measurement range switches, the first ``settle_samples`` samples
    afterwards are replaced by the last pre-switch value. This is a documented
    heuristic (experimental); the raw series is always preserved alongside so
    users can verify measurements or apply their own filter.
    """

    def __init__(self, settle_samples: int = 3) -> None:
        if settle_samples < 1:
            raise ValueError("settle_samples must be >= 1")
        self.settle_samples = settle_samples
        self._last_range: int | None = None
        self._hold_value: float | None = None
        self._hold_remaining = 0

    def reset(self) -> None:
        self._last_range = None
        self._hold_value = None
        self._hold_remaining = 0

    def notify_gap(self) -> None:
        # Filter state is not valid across missing data.
        self.reset()

    def apply(self, currents_ua: list[float], ranges: bytes) -> list[float]:
        out: list[float] = []
        for value, r in zip(currents_ua, ranges, strict=True):
            if self._last_range is not None and r != self._last_range:
                self._hold_remaining = self.settle_samples
            self._last_range = r
            if self._hold_remaining > 0 and self._hold_value is not None:
                out.append(self._hold_value)
                self._hold_remaining -= 1
            else:
                out.append(value)
                if not math.isnan(value):
                    self._hold_value = value
        return out
