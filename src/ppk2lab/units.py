"""Parsing and formatting for explicit engineering units.

The safety contract requires explicit units at API boundaries (for example
``voltage_mv``). These helpers parse human-entered CLI values like ``"5s"``,
``"20ms"``, ``"10uA"`` into canonical numbers, and never accept ambiguous
bare values where a unit is required.
"""

from __future__ import annotations

import re

from .errors import UsageError

_DURATION_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*(s|ms|us|min)?\s*$")

_CURRENT_UNITS_UA = {"na": 1e-3, "ua": 1.0, "ma": 1e3, "a": 1e6}
_CHARGE_UNITS_UC = {"nc": 1e-3, "uc": 1.0, "mc": 1e3, "c": 1e6}
_ENERGY_UNITS_UJ = {"nj": 1e-3, "uj": 1.0, "mj": 1e3, "j": 1e6}

_VALUE_UNIT_RE = re.compile(r"^\s*(-?[0-9]*\.?[0-9]+)\s*([a-zA-Zµ]+)\s*$")


def parse_duration_s(text: str) -> float:
    """Parse ``"5s"``, ``"20ms"``, ``"1.5min"`` or bare seconds into seconds."""
    match = _DURATION_RE.match(text)
    if not match:
        raise UsageError(f"Invalid duration: {text!r} (expected e.g. 5s, 200ms, 1.5min)")
    value = float(match.group(1))
    unit = match.group(2) or "s"
    scale = {"s": 1.0, "ms": 1e-3, "us": 1e-6, "min": 60.0}[unit]
    seconds = value * scale
    if seconds <= 0:
        raise UsageError(f"Duration must be positive: {text!r}")
    return seconds


def _normalize_unit(unit: str) -> str:
    return unit.replace("µ", "u").lower()


def parse_current_ua(text: str) -> float:
    """Parse a current with an explicit unit (nA/uA/mA/A) into microamps."""
    match = _VALUE_UNIT_RE.match(text)
    if not match:
        raise UsageError(f"Invalid current: {text!r} (expected e.g. 10uA, 1.5mA)")
    unit = _normalize_unit(match.group(2))
    if unit not in _CURRENT_UNITS_UA:
        raise UsageError(f"Unknown current unit in {text!r} (use nA, uA, mA, or A)")
    return float(match.group(1)) * _CURRENT_UNITS_UA[unit]


def parse_charge_uc(text: str) -> float:
    """Parse a charge with an explicit unit (nC/uC/mC/C) into microcoulombs."""
    match = _VALUE_UNIT_RE.match(text)
    if not match:
        raise UsageError(f"Invalid charge: {text!r} (expected e.g. 5uC)")
    unit = _normalize_unit(match.group(2))
    if unit not in _CHARGE_UNITS_UC:
        raise UsageError(f"Unknown charge unit in {text!r} (use nC, uC, mC, or C)")
    return float(match.group(1)) * _CHARGE_UNITS_UC[unit]


def parse_energy_uj(text: str) -> float:
    """Parse an energy with an explicit unit (nJ/uJ/mJ/J) into microjoules."""
    match = _VALUE_UNIT_RE.match(text)
    if not match:
        raise UsageError(f"Invalid energy: {text!r} (expected e.g. 100uJ)")
    unit = _normalize_unit(match.group(2))
    if unit not in _ENERGY_UNITS_UJ:
        raise UsageError(f"Unknown energy unit in {text!r} (use nJ, uJ, mJ, or J)")
    return float(match.group(1)) * _ENERGY_UNITS_UJ[unit]


def parse_channel(name: str) -> int:
    """Parse a digital channel name (``D0``-``D7``) into its bit index."""
    text = name.strip().upper()
    if len(text) == 2 and text[0] == "D" and text[1].isdigit():
        bit = int(text[1])
        if 0 <= bit <= 7:
            return bit
    raise UsageError(f"Invalid digital channel: {name!r} (expected D0-D7)")


def parse_channel_set(spec: str) -> list[int]:
    """Parse ``"D0-D7"``, ``"D0,D3"``, or single names into bit indexes."""
    bits: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo_text, hi_text = part.split("-", 1)
            lo, hi = parse_channel(lo_text), parse_channel(hi_text)
            if hi < lo:
                raise UsageError(f"Invalid channel range: {part!r}")
            bits.extend(range(lo, hi + 1))
        else:
            bits.append(parse_channel(part))
    if not bits:
        raise UsageError(f"No digital channels in {spec!r}")
    return sorted(set(bits))


def format_si(value_base_micro: float, base_unit: str) -> str:
    """Format a value expressed in micro-units with a readable SI prefix."""
    magnitude = abs(value_base_micro)
    if magnitude >= 1e6:
        return f"{value_base_micro / 1e6:.6g} {base_unit}"
    if magnitude >= 1e3:
        return f"{value_base_micro / 1e3:.6g} m{base_unit}"
    if magnitude >= 1 or magnitude == 0:
        return f"{value_base_micro:.6g} u{base_unit}"
    return f"{value_base_micro * 1e3:.6g} n{base_unit}"
