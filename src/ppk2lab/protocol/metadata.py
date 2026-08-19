"""Parser for the PPK2 metadata reply (opcode 0x19).

The device answers with newline-delimited ``KEY: VALUE`` text terminated by an
``END`` line. The parser is deliberately tolerant: unknown keys, reordering,
CRLF/LF, ``NaN`` values, and truncated replies are preserved and reported —
never treated as fatal, and never silently replaced with defaults.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

_LINE_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_]*)\s*:\s*(.*?)\s*$")
_INDEXED_RE = re.compile(r"^(R|GS|GI|O|S|I|UG)([0-4])$")

#: Calibration constant families indexed by measurement range 0-4.
CAL_FAMILIES = ("R", "GS", "GI", "O", "S", "I", "UG")


def _parse_number(text: str) -> float | None:
    """Parse a metadata numeric value; unparseable values become None."""
    try:
        value = float(text)
    except ValueError:
        return None
    return value  # NaN is preserved as float('nan'), not rejected


@dataclass
class Metadata:
    """Parsed PPK2 metadata plus the raw reply text as evidence."""

    raw_text: str
    #: Per-range calibration constants; None where the device did not report
    #: a value (unknown values are preserved, not defaulted).
    cal: dict[str, list[float | None]] = field(
        default_factory=lambda: {family: [None] * 5 for family in CAL_FAMILIES}
    )
    calibrated: bool | None = None
    vdd_mv: int | None = None
    hw: int | None = None
    mode: int | None = None
    ia: float | None = None
    #: Keys the parser did not recognize, preserved verbatim.
    extras: dict[str, str] = field(default_factory=dict)
    #: True when the END terminator was seen.
    terminated: bool = False
    warnings: list[str] = field(default_factory=list)

    def cal_value(self, family: str, range_index: int) -> float | None:
        return self.cal[family][range_index]

    def missing_cal_ranges(self) -> list[int]:
        """Ranges for which at least one calibration constant is missing/NaN."""
        missing = []
        for r in range(5):
            for family in CAL_FAMILIES:
                value = self.cal[family][r]
                if value is None or (isinstance(value, float) and math.isnan(value)):
                    missing.append(r)
                    break
        return missing

    def to_json(self) -> dict[str, Any]:
        def clean(v: float | None) -> float | None:
            if v is None or (isinstance(v, float) and math.isnan(v)):
                return None
            return v

        return {
            "calibrated": self.calibrated,
            "vdd_mv": self.vdd_mv,
            "hw": self.hw,
            "mode": self.mode,
            "ia": clean(self.ia),
            "calibration": {
                family: [clean(v) for v in values] for family, values in self.cal.items()
            },
            "extras": dict(self.extras),
            "terminated": self.terminated,
            "warnings": list(self.warnings),
        }


def parse_metadata(text: str) -> Metadata:
    """Parse a metadata reply. Never raises on malformed content; issues are
    reported through ``Metadata.warnings`` and missing fields stay ``None``."""
    meta = Metadata(raw_text=text)
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        if line == "END":
            meta.terminated = True
            break
        match = _LINE_RE.match(line)
        if not match:
            meta.warnings.append(f"unparseable metadata line: {line!r}")
            continue
        key, value = match.group(1), match.group(2)
        indexed = _INDEXED_RE.match(key)
        if indexed:
            family, idx = indexed.group(1), int(indexed.group(2))
            number = _parse_number(value)
            if number is None:
                meta.warnings.append(f"non-numeric calibration value {key}: {value!r}")
            meta.cal[family][idx] = number
        elif key == "Calibrated":
            number = _parse_number(value)
            meta.calibrated = bool(int(number)) if number is not None else None
        elif key == "VDD":
            number = _parse_number(value)
            meta.vdd_mv = int(number) if number is not None else None
        elif key == "HW":
            number = _parse_number(value)
            meta.hw = int(number) if number is not None else None
        elif key == "mode":
            number = _parse_number(value)
            meta.mode = int(number) if number is not None else None
        elif key == "IA":
            meta.ia = _parse_number(value)
        else:
            meta.extras[key] = value
    if not meta.terminated:
        meta.warnings.append("metadata reply not terminated by END (possibly truncated)")
    return meta
