"""Machine-readable warnings.

Errors already carry a stable ``code`` an agent can branch on; warnings
deserve the same treatment. A capture that says "3 sample gaps" in prose is
useless to a program deciding whether to retry, and prose is exactly the kind
of thing that gets reworded. Every warning therefore pairs a frozen code with
the human sentence.

Codes are stable per schema version; new codes may be added, existing ones
never change meaning.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

# -- capture integrity -------------------------------------------------------
#: The capture contains sample gaps; integrals are lower bounds.
W_SAMPLE_GAPS = "W_SAMPLE_GAPS"
#: The sample timeline advanced slower than the wall clock: samples were lost
#: beyond what the 6-bit counter can report.
W_TIMELINE_COMPRESSION = "W_TIMELINE_COMPRESSION"
#: Byte-level framing was lost and re-established; data around it is suspect.
W_STREAM_DESYNC = "W_STREAM_DESYNC"
#: Samples converted to a physically impossible current and were excluded.
W_IMPLAUSIBLE_SAMPLES = "W_IMPLAUSIBLE_SAMPLES"
#: The capture stored no samples at all.
W_NO_SAMPLES = "W_NO_SAMPLES"
#: The stream was interrupted; partial data was preserved.
W_INTERRUPTED = "W_INTERRUPTED"

# -- measurement trust -------------------------------------------------------
#: Energy could not be derived because no defensible supply voltage is known.
W_VOLTAGE_ASSUMED = "W_VOLTAGE_ASSUMED"
#: The device reports it is not calibrated.
W_NOT_CALIBRATED = "W_NOT_CALIBRATED"
#: Calibration constants are missing for one or more measurement ranges.
W_CALIBRATION_INCOMPLETE = "W_CALIBRATION_INCOMPLETE"
#: A non-unity user gain scales every reading in the affected ranges.
W_USER_GAIN = "W_USER_GAIN"
#: Device metadata could not be fully parsed.
W_METADATA = "W_METADATA"

# -- device state ------------------------------------------------------------
#: DUT power state is unknown or off; readings may legitimately be near zero.
W_DUT_POWER_UNKNOWN = "W_DUT_POWER_UNKNOWN"
#: A state change was requested but its result could not be read back.
W_STATE_UNVERIFIED = "W_STATE_UNVERIFIED"
#: Nothing was applied because the command ran as a dry run.
W_DRY_RUN = "W_DRY_RUN"
#: A previous session had left the device streaming; stale data was discarded.
W_SESSION_RECOVERED = "W_SESSION_RECOVERED"

# -- analysis ----------------------------------------------------------------
#: A decoder is running outside its validated rate tier.
W_DECODER_RATE = "W_DECODER_RATE"
#: A trigger never fired.
W_TRIGGER = "W_TRIGGER"
#: No device matched the discovery filter.
W_DEVICE_NOT_FOUND = "W_DEVICE_NOT_FOUND"
#: Anything that does not warrant its own code yet.
W_GENERIC = "W_GENERIC"


@dataclass(frozen=True)
class Diagnostic:
    """One warning: a stable code plus the sentence a human should read."""

    code: str
    message: str

    def to_json(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message}

    def __str__(self) -> str:  # human output and log lines
        return self.message


def warn(code: str, message: str) -> Diagnostic:
    """Shorthand constructor, kept short because call sites are dense."""
    return Diagnostic(code=code, message=message)


def as_json(warnings: Sequence[Any]) -> list[dict[str, Any]]:
    """Normalize a warning list to the JSON contract.

    Plain strings are accepted so that call sites can be migrated gradually;
    they surface as :data:`W_GENERIC`.
    """
    out: list[dict[str, Any]] = []
    for item in warnings:
        if isinstance(item, Diagnostic):
            out.append(item.to_json())
        elif isinstance(item, dict) and "code" in item and "message" in item:
            # Already in contract form (e.g. read back from a capture file).
            out.append({"code": str(item["code"]), "message": str(item["message"])})
        else:
            out.append({"code": W_GENERIC, "message": str(item)})
    return out
