"""Protocol-rate feasibility against the fixed 100 kS/s capture grid.

Support tiers (frozen for 0.2.0, hardware-validated thresholds tracked in
ROADMAP.md):

- ``validated``     >= 10 samples per bit/cycle (UART <= 9600 baud, SPI <= 10 kHz)
- ``conditional``   >= 5 samples  (UART 19200, SPI <= 20 kHz) — warning attached
- ``experimental``  >= 2.5 samples (UART 38400, SPI <= 40 kHz) — must be
  explicitly enabled and results carry low confidence
- ``unsupported``   below 2.5 samples — refused; no confident decode exists
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

from ..errors import DecoderRateError
from ..types import SAMPLE_RATE_HZ


class Tier(enum.Enum):
    VALIDATED = "validated"
    CONDITIONAL = "conditional"
    EXPERIMENTAL = "experimental"
    UNSUPPORTED = "unsupported"


#: Confidence assigned to clean decodes at each tier.
TIER_CONFIDENCE = {
    Tier.VALIDATED: 1.0,
    Tier.CONDITIONAL: 0.7,
    Tier.EXPERIMENTAL: 0.4,
    Tier.UNSUPPORTED: 0.0,
}

VALIDATED_MIN = 10.0
CONDITIONAL_MIN = 5.0
EXPERIMENTAL_MIN = 2.5


@dataclass
class Feasibility:
    tier: Tier
    samples_per_unit: float
    unit: str
    warnings: list[str] = field(default_factory=list)

    @property
    def confidence(self) -> float:
        return TIER_CONFIDENCE[self.tier]

    def to_json(self) -> dict[str, object]:
        return {
            "tier": self.tier.value,
            "samples_per_" + self.unit: self.samples_per_unit,
            "confidence": self.confidence,
            "warnings": list(self.warnings),
        }


def _classify(samples_per_unit: float, unit: str, label: str) -> Feasibility:
    if samples_per_unit >= VALIDATED_MIN:
        tier = Tier.VALIDATED
        warnings = []
    elif samples_per_unit >= CONDITIONAL_MIN:
        tier = Tier.CONDITIONAL
        warnings = [
            f"{label}: {samples_per_unit:.2f} samples per {unit} is in the conditional "
            "tier; error rates have not been hardware-validated at this rate"
        ]
    elif samples_per_unit >= EXPERIMENTAL_MIN:
        tier = Tier.EXPERIMENTAL
        warnings = [
            f"{label}: {samples_per_unit:.2f} samples per {unit} is experimental; "
            "phase and jitter failures are likely"
        ]
    else:
        tier = Tier.UNSUPPORTED
        warnings = [
            f"{label}: {samples_per_unit:.2f} samples per {unit} cannot be decoded "
            "from a 100 kS/s capture"
        ]
    return Feasibility(tier=tier, samples_per_unit=samples_per_unit, unit=unit, warnings=warnings)


def uart_feasibility(baud: int, sample_rate: int = SAMPLE_RATE_HZ) -> Feasibility:
    if baud <= 0:
        raise DecoderRateError(f"baud must be positive, got {baud}")
    return _classify(sample_rate / baud, "bit", f"UART {baud} baud")


def spi_feasibility(clock_hz: float, sample_rate: int = SAMPLE_RATE_HZ) -> Feasibility:
    if clock_hz <= 0:
        raise DecoderRateError(f"SPI clock must be positive, got {clock_hz}")
    return _classify(sample_rate / clock_hz, "cycle", f"SPI {clock_hz:g} Hz")


def require_supported(feasibility: Feasibility, *, allow_experimental: bool) -> Feasibility:
    """Enforce the tier policy; conditional passes with warnings attached."""
    if feasibility.tier is Tier.UNSUPPORTED:
        raise DecoderRateError(feasibility.warnings[0])
    if feasibility.tier is Tier.EXPERIMENTAL and not allow_experimental:
        raise DecoderRateError(
            feasibility.warnings[0] + " (pass allow_experimental/--allow-experimental to "
            "decode anyway with low confidence)"
        )
    return feasibility
