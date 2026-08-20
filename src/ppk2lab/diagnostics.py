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
#: Wall-clock elapsed time accounts for more samples than the timeline does,
#: by more than the anchoring and clock-rate floor can explain.
W_UNACCOUNTED_SAMPLES = "W_UNACCOUNTED_SAMPLES"
#: The requested window extends past where the capture has data.
W_WINDOW_UNPOPULATED = "W_WINDOW_UNPOPULATED"
#: The gap table hit its enumeration ceiling; gaps exist that it does not list.
W_GAP_TABLE_TRUNCATED = "W_GAP_TABLE_TRUNCATED"
#: Samples sat on the ADC's full-scale code: the reading is pinned, not measured.
W_CLIPPED = "W_CLIPPED"
#: A stored manifest states something the capture itself cannot support.
W_MANIFEST_IMPLAUSIBLE = "W_MANIFEST_IMPLAUSIBLE"
#: Only part of the capture was read, so the whole-file digest is unverified.
W_PARTIAL_INTEGRITY = "W_PARTIAL_INTEGRITY"

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
#: A reported quantile sits at the distribution grid floor: it bounds the
#: true value from above rather than measuring it.
W_BELOW_MEASUREMENT_FLOOR = "W_BELOW_MEASUREMENT_FLOOR"

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
#: The output is a decimated summary, not the raw per-sample series.
W_DECIMATED = "W_DECIMATED"
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


#: What each code means, published through ``ppk2lab capabilities --json`` so
#: an agent can learn the vocabulary from the tool instead of the source.
WARNING_CATALOG: dict[str, str] = {
    W_SAMPLE_GAPS: "The capture contains sample gaps; integrals are lower bounds.",
    W_TIMELINE_COMPRESSION: (
        "The sample timeline advanced far slower than the wall clock: samples were lost "
        "beyond what the 6-bit counter can report."
    ),
    W_STREAM_DESYNC: "Byte-level framing was lost and re-established; data around it is suspect.",
    W_IMPLAUSIBLE_SAMPLES: (
        "Samples converted to a current the hardware cannot carry and were excluded."
    ),
    W_NO_SAMPLES: "The capture stored no samples at all.",
    W_INTERRUPTED: "The stream was interrupted; partial data was preserved.",
    W_UNACCOUNTED_SAMPLES: (
        "Wall-clock elapsed time accounts for more samples than the timeline does, by more "
        "than host anchoring and clock rate can explain. The loss is real but belongs to the "
        "whole capture: nothing localizes it to a window, so coverage cannot show it."
    ),
    W_WINDOW_UNPOPULATED: (
        "The requested window extends past where the capture has data; the positions beyond "
        "it hold neither a sample nor a recorded gap, so every integral over the window is a "
        "lower bound."
    ),
    W_GAP_TABLE_TRUNCATED: (
        "The gap table hit its enumeration ceiling. The gap count stays exact, but gaps exist "
        "that the table does not list, so timeline positions cannot all be resolved."
    ),
    W_CLIPPED: (
        "Samples sat on the ADC's full-scale code. In the top range the DUT exceeded the "
        "instrument's 1 A span; in a lower range the auto-range switch had not completed. "
        "Either way those amplitudes are understated and the integral is a lower bound."
    ),
    W_MANIFEST_IMPLAUSIBLE: (
        "A stored manifest states something the capture itself cannot support. The value is "
        "reported as stored rather than corrected — a plausible substitute would be invented."
    ),
    W_PARTIAL_INTEGRITY: (
        "Only part of the capture was read, so only the chunks it touched had their CRC32 "
        "verified. The manifest's SHA-256 over the whole file was not checked and is "
        "reported as null rather than as verified."
    ),
    W_VOLTAGE_ASSUMED: (
        "Energy could not be derived because no defensible supply voltage is known. Pass "
        "the DUT's real supply voltage to compute it."
    ),
    W_NOT_CALIBRATED: "The device reports it is not calibrated; absolute accuracy is unconfirmed.",
    W_CALIBRATION_INCOMPLETE: (
        "Calibration constants are missing for one or more ranges; those samples do not convert."
    ),
    W_USER_GAIN: "A non-unity user gain scales every reading in the affected ranges.",
    W_METADATA: "Device metadata could not be fully parsed.",
    W_BELOW_MEASUREMENT_FLOOR: (
        "Enough samples fell below the distribution grid's floor that a reported quantile "
        "is served from the floor itself. The grid starts at the finest step the most "
        "sensitive range can resolve and is logarithmic, so it cannot represent a reading "
        "at or below zero — and an unloaded input legitimately produces those. The affected "
        "quantiles bound the true value from above; they do not measure it."
    ),
    W_DUT_POWER_UNKNOWN: (
        "DUT power state is unknown or off; readings may legitimately be near zero."
    ),
    W_STATE_UNVERIFIED: "A state change was applied but its result could not be read back.",
    W_DRY_RUN: "Nothing was applied because the command ran as a dry run.",
    W_SESSION_RECOVERED: (
        "A previous session had left the device streaming; stale data was discarded."
    ),
    W_DEVICE_NOT_FOUND: "No device matched the discovery filter.",
    W_DECODER_RATE: "A decoder is running outside its validated rate tier.",
    W_TRIGGER: "A trigger never fired.",
    W_DECIMATED: (
        "The output is a decimated summary with its own record shape, not the raw "
        "per-sample series. Each bucket reports how many of its samples were present, "
        "so a bucket over a gap cannot pass for a full one; the artifact remains the "
        "evidence."
    ),
    W_GENERIC: "A condition that does not warrant its own code yet.",
}


#: Which part of a result a code speaks about. Published alongside the
#: meaning so an agent can route a warning without pattern-matching its name.
#: Deliberately not a severity: how much a warning matters depends on the
#: question being asked, and freezing an editorial judgment into the contract
#: would answer it for everyone.
WARNING_CATEGORY: dict[str, str] = {
    W_SAMPLE_GAPS: "capture integrity",
    W_TIMELINE_COMPRESSION: "capture integrity",
    W_STREAM_DESYNC: "capture integrity",
    W_IMPLAUSIBLE_SAMPLES: "capture integrity",
    W_NO_SAMPLES: "capture integrity",
    W_INTERRUPTED: "capture integrity",
    W_UNACCOUNTED_SAMPLES: "capture integrity",
    W_WINDOW_UNPOPULATED: "capture integrity",
    W_GAP_TABLE_TRUNCATED: "capture integrity",
    W_CLIPPED: "capture integrity",
    W_MANIFEST_IMPLAUSIBLE: "capture integrity",
    W_PARTIAL_INTEGRITY: "capture integrity",
    W_VOLTAGE_ASSUMED: "measurement trust",
    W_NOT_CALIBRATED: "measurement trust",
    W_CALIBRATION_INCOMPLETE: "measurement trust",
    W_USER_GAIN: "measurement trust",
    W_METADATA: "measurement trust",
    W_BELOW_MEASUREMENT_FLOOR: "measurement trust",
    W_DUT_POWER_UNKNOWN: "device state",
    W_STATE_UNVERIFIED: "device state",
    W_DRY_RUN: "device state",
    W_SESSION_RECOVERED: "device state",
    W_DECODER_RATE: "analysis",
    W_TRIGGER: "analysis",
    W_DECIMATED: "analysis",
    W_DEVICE_NOT_FOUND: "analysis",
    W_GENERIC: "analysis",
}


#: Why a gap appears in a capture's gap table. Published as an **open**
#: catalog, in the same ``{code, meaning}`` shape as the warning codes: a new
#: reason is a new way the hardware or host can lose samples, and closing this
#: into an enum would make discovering one a breaking contract change.
#: Where the loss happened, for each gap reason. Same purpose as
#: :data:`WARNING_CATEGORY`: it lets one reader handle all three catalogs, and
#: it is the first thing worth knowing about a gap — a host-side loss is
#: fixable by the operator, a device-side one is not.
GAP_CATEGORY: dict[str, str] = {
    "counter_skip": "device or transit",
    "host_overflow": "host",
    "usb_stall": "transport",
    "stream_desync": "framing",
    "discontinuous_feed": "analysis",
    "sample_gap": "analysis",
}

#: What ended the capture, for each interruption reason.
INTERRUPTION_CATEGORY: dict[str, str] = {
    "keyboard_interrupt": "operator",
    "transport_error": "transport",
    "stream_stalled": "device",
    "trigger_timeout": "trigger",
    "trigger_never_fired": "trigger",
    "timeline_compression": "capture integrity",
}

GAP_REASONS: dict[str, str] = {
    "counter_skip": (
        "The device's 6-bit sample counter jumped: samples were lost on the device or in "
        "transit. The count is exact modulo 64, so it can understate the loss by a multiple "
        "of 64 and never overstate it."
    ),
    "host_overflow": (
        "The host's bounded stream queue dropped whole chunks; the exact byte count is known "
        "and converted to samples."
    ),
    "usb_stall": "The USB stream stopped delivering data for an unknown number of samples.",
    "stream_desync": (
        "Byte-level framing was lost and re-established. The 4-byte sample words have no sync "
        "word, so the number of samples spanned is unknown."
    ),
    "discontinuous_feed": (
        "A decoder was fed sample data that does not continue where the previous feed ended; "
        "the decoder fences its state at the discontinuity rather than joining across it."
    ),
    "sample_gap": (
        "A decoder annotation marking the samples a gap removed, so a gap between frames is "
        "visible in decoded output rather than closing silently."
    ),
}

#: Why a capture stopped short of what was asked for. Open catalog, same shape
#: and same reasoning as :data:`GAP_REASONS`.
INTERRUPTION_REASONS: dict[str, str] = {
    "keyboard_interrupt": "The operator interrupted the capture; partial data was preserved.",
    "transport_error": "The serial transport failed (USB disconnect or I/O error).",
    "stream_stalled": "The device kept its serial port open but stopped streaming samples.",
    "trigger_timeout": "The trigger had not fired within --trigger-timeout, so the run aborted.",
    "trigger_never_fired": "The stream ended before the trigger condition was ever met.",
    "timeline_compression": (
        "The sample timeline advanced far slower than the wall clock: samples were lost beyond "
        "what the 6-bit counter can report, so durations and integrals understate reality."
    ),
}


def warning_catalog() -> list[dict[str, str]]:
    """Machine-readable catalog of every stable warning code."""
    return [
        {"code": code, "category": WARNING_CATEGORY[code], "meaning": meaning}
        for code, meaning in sorted(WARNING_CATALOG.items())
    ]


def gap_reason_catalog() -> list[dict[str, str]]:
    """Machine-readable catalog of gap reasons (open: new reasons may appear)."""
    return [
        {"code": code, "category": GAP_CATEGORY[code], "meaning": meaning}
        for code, meaning in sorted(GAP_REASONS.items())
    ]


def interruption_reason_catalog() -> list[dict[str, str]]:
    """Machine-readable catalog of interruption reasons (open, like gaps)."""
    return [
        {"code": code, "category": INTERRUPTION_CATEGORY[code], "meaning": meaning}
        for code, meaning in sorted(INTERRUPTION_REASONS.items())
    ]


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
