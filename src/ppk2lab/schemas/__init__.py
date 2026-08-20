"""Versioned JSON Schemas for every machine-readable contract.

``SCHEMA_VERSION`` (in ``ppk2lab._version``) is independent of the package
version. Schemas are defined here as the single source of truth; the CLI
``ppk2lab schema`` command serves them, and tests validate real command
output against them.
"""

from __future__ import annotations

from typing import Any

from .._version import SCHEMA_VERSION
from ..errors import SchemaNotFoundError

_DRAFT = "https://json-schema.org/draft/2020-12/schema"


def _obj(properties: dict[str, Any], required: list[str] | None = None, **kw: Any) -> dict:
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    schema.update(kw)
    return schema


_NULLABLE_NUMBER = {"type": ["number", "null"]}
_NULLABLE_INT = {"type": ["integer", "null"]}
_NULLABLE_STR = {"type": ["string", "null"]}
_NULLABLE_BOOL = {"type": ["boolean", "null"]}
_STR_ARRAY = {"type": "array", "items": {"type": "string"}}

#: A warning with a stable code an agent can branch on (see
#: ``ppk2lab.diagnostics``), alongside the sentence a human should read.
_DIAGNOSTIC = _obj(
    {
        "code": {"type": "string"},
        "message": {"type": "string"},
        # Which part of a result the warning is about, not how much it matters.
        "category": _NULLABLE_STR,
    },
    ["code", "message"],
)
_DIAGNOSTIC_ARRAY = {"type": "array", "items": _DIAGNOSTIC}

_GAP = _obj(
    {
        "index": {"type": "integer", "minimum": 0},
        "missing": _NULLABLE_INT,
        "reason": {"type": "string"},
        "ambiguous": {"type": "boolean"},
    },
    ["index", "missing", "reason", "ambiguous"],
)

_ERROR = _obj(
    {
        "code": {"type": "string"},
        "message": {"type": "string"},
        "remediation": {"type": "string"},
        "exit_code": {"type": "integer"},
    },
    ["code", "message", "remediation", "exit_code"],
)

_STATE = _obj(
    {
        "mode": _NULLABLE_STR,
        "source_voltage_mv": _NULLABLE_INT,
        "dut_power": _NULLABLE_BOOL,
        "measuring": {"type": "boolean"},
        "source_voltage_basis": {
            "enum": [
                "caller_override",
                "configured_source",
                "device_metadata",
                "unknown",
            ]
        },
    }
)

_STATE_CHANGE = _obj(
    {
        "operation": {"type": "string"},
        "requested": {"type": "object"},
        "before": {"type": "object"},
        "after": {"type": "object"},
        "applied": {"type": "boolean"},
        "observed_after": {"type": "boolean"},
        "warnings": _STR_ARRAY,
    },
    ["operation", "requested", "before", "after", "applied", "observed_after"],
)

_PORT = _obj(
    {
        "path": {"type": "string"},
        "role": {"enum": ["measurement", "shell", "unknown"]},
        "usb_location": _NULLABLE_STR,
        "interface_number": _NULLABLE_INT,
    },
    ["path", "role"],
)

_DEVICE = _obj(
    {
        "serial_number": _NULLABLE_STR,
        "vid": _NULLABLE_INT,
        "pid": _NULLABLE_INT,
        "ports": {"type": "array", "items": _PORT},
        "firmware_version": _NULLABLE_STR,
        "simulated": {"type": "boolean"},
    },
    ["serial_number", "ports", "simulated"],
)

_WINDOW_STATS = _obj(
    {
        "window": _obj(
            {"start_index": {"type": "integer"}, "end_index": {"type": "integer"}},
            ["start_index", "end_index"],
        ),
        "duration_s": {"type": "number"},
        "samples": _obj(
            {
                "stored": {"type": "integer"},
                "missing_known": {"type": "integer"},
                "has_unknown_gaps": {"type": "boolean"},
                "valid": {"type": "integer"},
                "invalid_range": {"type": "integer"},
                "not_convertible": {"type": "integer"},
                "implausible": {"type": "integer"},
                # Window positions holding neither a sample nor a recorded
                # gap: the window reaches past what the capture covers.
                "unpopulated": _NULLABLE_INT,
                "saturated": {"type": "integer"},
                "zero_code": {"type": "integer"},
                "covered_fraction": _NULLABLE_NUMBER,
            }
        ),
        "current_ua": _obj(
            {
                "mean": _NULLABLE_NUMBER,
                "min": _NULLABLE_NUMBER,
                "max": _NULLABLE_NUMBER,
                "peak_index": _NULLABLE_INT,
                # Quantiles describe a fraction of the distribution rather than
                # its extreme, so unlike `max` they do not drift upward as a
                # capture gets longer (range switches accumulate; the DUT does
                # not change). See docs/energy-analysis.md.
                "p5": _NULLABLE_NUMBER,
                "p50": _NULLABLE_NUMBER,
                "p90": _NULLABLE_NUMBER,
                "p95": _NULLABLE_NUMBER,
                "p99": _NULLABLE_NUMBER,
                "p999": _NULLABLE_NUMBER,
            }
        ),
        # The log-spaced grid the quantiles were read off. A quantile is
        # quantized to it, and readings below the floor have no bin, so a
        # quantile served from them is an upper bound.
        "distribution": _obj(
            {
                "grid_min_ua": {"type": "number"},
                "grid_max_ua": {"type": "number"},
                "bins_per_decade": {"type": "integer"},
                "quantile_half_width_fraction": {"type": "number"},
                "below_grid_samples": {"type": "integer"},
                "above_grid_samples": {"type": "integer"},
                # Reported quantiles served from the grid floor: they bound the
                # true value from above rather than measuring it.
                "quantiles_at_floor": {"type": "array", "items": {"type": "string"}},
            }
        ),
        # Null unless a state threshold was asked for: the split is a function
        # of the caller's choice, so a default would bake one editorial
        # boundary into every result.
        "state_split": {
            "anyOf": [
                {"type": "null"},
                _obj(
                    {
                        "threshold_ua": {"type": "number"},
                        "below": {"type": "object"},
                        "above": {"type": "object"},
                    },
                    ["threshold_ua", "below", "above"],
                ),
            ]
        },
        # Nordic publishes per-range accuracy as *typical*, not guaranteed, so
        # every field says so and `guaranteed` is always false. The per-range
        # figures are systematic gain specifications: fully correlated inside a
        # range, summed linearly, never divided by sqrt(N). The statistical
        # uncertainty of the mean is separate and separately labelled, because
        # 10 us samples through a shunt-switching front end are autocorrelated.
        "uncertainty": {
            "anyOf": [
                {"type": "null"},
                _obj(
                    {
                        "model": {"type": "string"},
                        "guaranteed": {"const": False},
                        "mean_ua_typical": _NULLABLE_NUMBER,
                        "charge_uc_typical": _NULLABLE_NUMBER,
                        "energy_uj_typical": _NULLABLE_NUMBER,
                        "mean_ua_batch_stderr": _NULLABLE_NUMBER,
                        "batch_count": _NULLABLE_INT,
                        "batch_samples": _NULLABLE_INT,
                    },
                    ["model", "guaranteed"],
                ),
            ]
        },
        "charge_uc": _NULLABLE_NUMBER,
        "charge_is_lower_bound": {"type": "boolean"},
        "energy_uj": _NULLABLE_NUMBER,
        "source_voltage_mv": _NULLABLE_INT,
        # The PPK2 never measures the DUT terminal voltage: energy is always
        # charge x an assumed voltage, and this records which assumption.
        "voltage_basis": {
            "enum": ["caller_override", "configured_source", "device_metadata", "unknown"]
        },
        "voltage_measured": {"const": False},
        "energy_note": _NULLABLE_STR,
        "complete": {"type": "boolean"},
        # The instrument's ceiling: a sample sitting on the ADC's full-scale
        # code is pinned, so its amplitude is a lower bound whatever the
        # calibrated value says.
        "saturated_samples": {"type": "integer"},
        "saturated_ranges": {"type": "array", "items": {"type": "integer"}},
        "samples_per_range": {"type": "array", "items": {"type": "integer"}},
        "charge_per_range_uc": {"type": ["array", "null"], "items": {"type": "number"}},
        "range_switches": {"type": "integer"},
        "range_switch_rate_hz": _NULLABLE_NUMBER,
        # Capture-level: the wall clock witnesses loss the 6-bit counter
        # cannot describe, and nothing localizes it to this window.
        "unaccounted_samples_estimate": _NULLABLE_INT,
        "unaccounted_loss_is_capture_level": {"type": "boolean"},
        "sample_gaps": {"type": "array", "items": _GAP},
        "gaps_truncated": {"type": "integer"},
    },
    [
        "window",
        "duration_s",
        "samples",
        "current_ua",
        "complete",
        "sample_gaps",
        "voltage_basis",
        "voltage_measured",
    ],
)

#: Wall-clock cross-check of the sample timeline. The 6-bit sample counter
#: cannot describe losses of 64 or more samples (and a loss of exactly k*64 is
#: invisible to it), so elapsed wall time is the only independent witness.
_TIMELINE_CHECK = _obj(
    {
        "started_utc": {"type": "string"},
        "ended_utc": {"type": "string"},
        # Stamped when the first block was delivered, not when its first
        # sample was taken; anchor_uncertainty_s bounds the difference.
        "first_sample_utc": _NULLABLE_STR,
        "anchor_uncertainty_s": _NULLABLE_NUMBER,
        "wall_elapsed_s": _NULLABLE_NUMBER,
        "timeline_advance": {"type": "integer"},
        "achieved_sample_rate_hz": _NULLABLE_NUMBER,
        "rate_deficit_ratio": _NULLABLE_NUMBER,
        # Signed companion to rate_deficit_ratio: a positive value means the
        # timeline outran wall time, which is an anchoring artifact the
        # clamped figure hides.
        "rate_offset_ratio": _NULLABLE_NUMBER,
        "unaccounted_samples_estimate": _NULLABLE_INT,
        "unaccounted_floor_samples": _NULLABLE_INT,
        "rate_check": {"enum": ["ok", "deficit", "too_short", "not_applicable"]},
    },
    ["rate_check", "timeline_advance"],
)

_ANNOTATION = _obj(
    {
        "decoder": {"type": "string"},
        "kind": {"type": "string"},
        "start_sample": {"type": "integer"},
        "end_sample": {"type": "integer"},
        "fields": {"type": "object"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "errors": _STR_ARRAY,
    },
    ["decoder", "kind", "start_sample", "end_sample", "fields", "confidence", "errors"],
)

#: Why a capture stopped short. ``reason`` is deliberately an open string, not
#: an enum: a new reason is a new way a capture can end, and closing this would
#: make discovering one a breaking contract change. The meanings are published
#: as ``interruption_reasons`` by ``ppk2lab capabilities``.
_INTERRUPTION = _obj(
    {
        "reason": {"type": "string"},
        "detail": _NULLABLE_STR,
    },
    ["reason"],
)

_NULLABLE_INTERRUPTION: dict[str, Any] = {"anyOf": [{"type": "null"}, _INTERRUPTION]}


#: Caller-supplied provenance recorded with a capture. Strings only, so what
#: comes back out is exactly the text that went in.
_USER_TAGS: dict[str, Any] = {
    "type": "object",
    "additionalProperties": {"type": "string"},
}

#: A scheduled action and the sample index it actually fired at. An action the
#: capture ended before reaching is recorded too, with both index fields null
#: and the reason in ``error`` — a stimulus that never happened is a fact about
#: the run, so the keys stay present rather than the record being dropped.
_SCHEDULED_ACTIONS: dict[str, Any] = {
    "type": "array",
    "items": _obj(
        {
            "label": {"type": "string"},
            "requested_s": {"type": "number"},
            "fired_index": _NULLABLE_INT,
            "fired_s": _NULLABLE_NUMBER,
            "error": _NULLABLE_STR,
        }
    ),
}

SCHEMAS: dict[str, dict[str, Any]] = {
    "envelope": _obj(
        {
            "schema_version": {"const": SCHEMA_VERSION},
            "command": {"type": "string"},
            "ok": {"type": "boolean"},
            "result": {"type": ["object", "null"]},
            "warnings": _DIAGNOSTIC_ARRAY,
            "error": {"anyOf": [{"type": "null"}, _ERROR]},
        },
        ["schema_version", "command", "ok", "result", "warnings", "error"],
        **{
            "$schema": _DRAFT,
            "$id": "ppk2lab:envelope",
            "title": "CLI JSON envelope shared by every command",
        },
    ),
    "error": dict(_ERROR, **{"$schema": _DRAFT, "$id": "ppk2lab:error"}),
    "diagnostic": dict(_DIAGNOSTIC, **{"$schema": _DRAFT, "$id": "ppk2lab:diagnostic"}),
    "device": dict(_DEVICE, **{"$schema": _DRAFT, "$id": "ppk2lab:device"}),
    "state-change": dict(_STATE_CHANGE, **{"$schema": _DRAFT, "$id": "ppk2lab:state-change"}),
    "gap": dict(_GAP, **{"$schema": _DRAFT, "$id": "ppk2lab:gap"}),
    "annotation": dict(_ANNOTATION, **{"$schema": _DRAFT, "$id": "ppk2lab:annotation"}),
    "window-stats": dict(_WINDOW_STATS, **{"$schema": _DRAFT, "$id": "ppk2lab:window-stats"}),
    "discover-result": _obj(
        {"devices": {"type": "array", "items": _DEVICE}},
        ["devices"],
        **{"$schema": _DRAFT, "$id": "ppk2lab:discover-result"},
    ),
    "info-result": _obj(
        {
            "device": _DEVICE,
            "state": _STATE,
            "metadata": {"type": "object"},
            "calibration_missing_ranges": {"type": "array", "items": {"type": "integer"}},
        },
        ["device", "state", "metadata"],
        **{"$schema": _DRAFT, "$id": "ppk2lab:info-result"},
    ),
    "capabilities-result": _obj(
        {
            "device": _obj(
                {
                    "sample_rate_hz": {"const": 100000},
                    "digital_channels": _STR_ARRAY,
                    "modes": _STR_ARRAY,
                    "source_voltage_mv": _obj(
                        {"min": {"type": "integer"}, "max": {"type": "integer"}}
                    ),
                }
            ),
            "commands": {
                "type": "array",
                "items": _obj(
                    {
                        "name": {"type": "string"},
                        "state_changing": {"type": "boolean"},
                        "description": {"type": "string"},
                        "options": {"type": "array"},
                    },
                    ["name", "state_changing"],
                ),
            },
            "decoders": {"type": "array", "items": {"type": "object"}},
            "exit_codes": {"type": "object"},
            "error_codes": {"type": "array", "items": {"type": "object"}},
            "warning_codes": {"type": "array", "items": {"type": "object"}},
            "schemas": _STR_ARRAY,
        },
        [
            "device",
            "commands",
            "decoders",
            "exit_codes",
            "error_codes",
            "warning_codes",
            "schemas",
        ],
        **{"$schema": _DRAFT, "$id": "ppk2lab:capabilities-result"},
    ),
    "doctor-result": _obj(
        {
            "checks": {
                "type": "array",
                "items": _obj(
                    {
                        "name": {"type": "string"},
                        "status": {"enum": ["pass", "fail", "warn", "skip"]},
                        "detail": {"type": "string"},
                        "remediation": _NULLABLE_STR,
                    },
                    ["name", "status", "detail"],
                ),
            },
            "summary": _obj(
                {
                    "pass": {"type": "integer"},
                    "fail": {"type": "integer"},
                    "warn": {"type": "integer"},
                    "skip": {"type": "integer"},
                }
            ),
        },
        ["checks", "summary"],
        **{"$schema": _DRAFT, "$id": "ppk2lab:doctor-result"},
    ),
    "configure-result": _obj(
        {
            "dry_run": {"type": "boolean"},
            "changes": {"type": "array", "items": _STATE_CHANGE},
            "state": _STATE,
        },
        ["dry_run", "changes", "state"],
        **{"$schema": _DRAFT, "$id": "ppk2lab:configure-result"},
    ),
    "timeline-check": dict(_TIMELINE_CHECK, **{"$schema": _DRAFT, "$id": "ppk2lab:timeline-check"}),
    "capture-result": _obj(
        {
            "capture_id": {"type": "string"},
            "capture_sha256": _NULLABLE_STR,
            "path": _NULLABLE_STR,
            "complete": {"type": "boolean"},
            "interruption": _NULLABLE_INTERRUPTION,
            "trigger": {"type": ["object", "null"]},
            "timeline": _TIMELINE_CHECK,
            "user_tags": _USER_TAGS,
            "scheduled_actions": _SCHEDULED_ACTIONS,
            "stats": {"anyOf": [{"type": "null"}, _WINDOW_STATS]},
            "warnings": _DIAGNOSTIC_ARRAY,
        },
        ["capture_id", "complete", "stats", "warnings", "timeline"],
        **{"$schema": _DRAFT, "$id": "ppk2lab:capture-result"},
    ),
    "inspect-result": _obj(
        {
            "path": {"type": "string"},
            "format": {"type": "string"},
            "format_version": {"type": "integer"},
            "capture_id": {"type": "string"},
            "created_utc": {"type": "string"},
            "device": {"type": "object"},
            "configuration": {"type": "object"},
            "user_tags": _USER_TAGS,
            "scheduled_actions": _SCHEDULED_ACTIONS,
            "timeline": _TIMELINE_CHECK,
            "duration_s": {"type": "number"},
            # A truncated gap table or an unknown-size gap makes the span a
            # floor: the manifest can only price the loss it enumerated.
            "duration_is_lower_bound": {"type": "boolean"},
            "samples": _obj(
                {
                    "encoding": {"type": "string"},
                    "stored": {"type": "integer"},
                    "missing_known": {"type": "integer"},
                    "invalid_range_count": {"type": ["integer", "null"]},
                    "chunk_count": {"type": "integer"},
                    "sha256": _NULLABLE_STR,
                    # Always false here: a manifest-only read never touches a
                    # sample chunk, so it cannot have verified the digest.
                    "sha256_verified": {"type": "boolean"},
                }
            ),
            "gap_count": {"type": "integer"},
            "gaps_truncated": {"type": "integer", "minimum": 0},
            "gaps_with_unknown_size": {"type": "integer"},
            "complete": {"type": "boolean"},
            "interruption": _NULLABLE_INTERRUPTION,
            "calibration": {"type": ["object", "null"]},
            "stats": {"type": ["object", "null"]},
            "warnings": {"type": "array"},
        },
        ["path", "capture_id", "samples", "gap_count", "complete"],
        **{"$schema": _DRAFT, "$id": "ppk2lab:inspect-result"},
    ),
    "decode-result": _obj(
        {
            "decoder": {"type": "object"},
            "annotation_count": {"type": "integer"},
            "output": _NULLABLE_STR,
            "annotations": {"type": ["array", "null"], "items": _ANNOTATION},
            "summary": {"type": "object"},
            "capture_id": {"type": "string"},
            "capture_sha256": {"type": "string"},
        },
        ["decoder", "annotation_count", "summary"],
        **{"$schema": _DRAFT, "$id": "ppk2lab:decode-result"},
    ),
    "measure-result": _obj(
        {
            "capture_id": {"type": "string"},
            "capture_sha256": {"type": "string"},
            "window": {"anyOf": [{"type": "null"}, _WINDOW_STATS]},
            "annotations": {"type": ["array", "null"]},
            "groups": {"type": ["array", "null"]},
        },
        ["capture_id", "capture_sha256"],
        **{"$schema": _DRAFT, "$id": "ppk2lab:measure-result"},
    ),
    "assert-result": _obj(
        {
            "outcomes": {
                "type": "array",
                "items": _obj(
                    {
                        "rule": {"type": "object"},
                        "status": {"enum": ["passed", "failed", "no_event", "incomplete"]},
                        "passed": {"type": "boolean"},
                        "events_found": {"type": "integer"},
                        "observations": {"type": "array"},
                        "capture_id": {"type": "string"},
                        "capture_sha256": {"type": "string"},
                        "warnings": _STR_ARRAY,
                    },
                    ["rule", "status", "passed", "observations"],
                ),
            },
            "passed": {"type": "boolean"},
        },
        ["outcomes", "passed"],
        **{"$schema": _DRAFT, "$id": "ppk2lab:assert-result"},
    ),
    "compare-result": _obj(
        {
            "metric": {"type": "string"},
            "unit": {"type": "string"},
            "a": {"type": "object"},
            "b": {"type": "object"},
            "a_value": _NULLABLE_NUMBER,
            "b_value": _NULLABLE_NUMBER,
            # candidate - baseline: the change from the first capture to the second.
            "delta": _NULLABLE_NUMBER,
            "relative": _NULLABLE_NUMBER,
            # same_range: both captures stayed in one shunt range, so the gain
            # error is a shared unknown and scales the difference. cross_range:
            # independent gains, which add. mixed: at least one capture switched
            # ranges, so no single gain factor describes it.
            "basis": {"enum": ["same_range", "cross_range", "mixed"]},
            "dominant_range": _obj(
                {"a": {"type": ["integer", "null"]}, "b": {"type": ["integer", "null"]}}
            ),
            # A gain error is one physical unit's unknown, so the cancellation
            # needs one physical unit. null = the captures did not identify
            # their instruments, so the premise is unverified rather than
            # known-false.
            "same_instrument": _NULLABLE_BOOL,
            # Present for voltage-dependent metrics only: energy is charge x V,
            # and two captures taken at different setpoints differ by the
            # supply as well as by the DUT.
            "voltage": _obj(
                {
                    "a": _NULLABLE_INT,
                    "b": _NULLABLE_INT,
                    "basis": _obj({"a": _NULLABLE_STR, "b": _NULLABLE_STR}),
                    "differs": {"type": "boolean"},
                    "note": _obj({"a": _NULLABLE_STR, "b": _NULLABLE_STR}),
                }
            ),
            "uncertainty": {
                "anyOf": [
                    {"type": "null"},
                    _obj(
                        {
                            "model": {"type": "string"},
                            "guaranteed": {"const": False},
                            "delta_typical": {"type": "number"},
                            "gain_term": {"type": "number"},
                            "resolution_term": {"type": "number"},
                            "gain_error_cancels": {"type": "boolean"},
                            "delta_batch_stderr": _NULLABLE_NUMBER,
                            "note": {"type": "string"},
                        }
                    ),
                ]
            },
            "uncertainty_note": {"type": "string"},
        },
        ["metric", "unit", "a", "b", "delta", "basis", "uncertainty"],
        **{"$schema": _DRAFT, "$id": "ppk2lab:compare-result"},
    ),
    "export-result": _obj(
        {
            "format": {"enum": ["csv", "vcd", "jsonl"]},
            "output": {"type": "string"},
            "records": {"type": "integer"},
            "capture_id": {"type": "string"},
            # Null for a windowed export: only the chunks the window touched
            # were CRC-checked, so publishing the whole-file digest would be
            # asserting a check that was not run (see W_PARTIAL_INTEGRITY).
            "capture_sha256": _NULLABLE_STR,
            "window": {
                "anyOf": [
                    {"type": "null"},
                    _obj(
                        {
                            "start_s": _NULLABLE_NUMBER,
                            "end_s": _NULLABLE_NUMBER,
                            "start_index": {"type": "integer"},
                            "end_index": {"type": "integer"},
                        }
                    ),
                ]
            },
            # Null for a raw export. A decimated export is a derived summary
            # with its own header and its own record shape; the two are never
            # produced by the same invocation (docs/decimation.md).
            "decimation": {
                "anyOf": [
                    {"type": "null"},
                    _obj(
                        {
                            "bucket_samples": {"type": "integer"},
                            "bucket_ms": _NULLABLE_NUMBER,
                            "buckets": {"type": "integer"},
                        },
                        ["bucket_samples", "buckets"],
                    ),
                ]
            },
            # Measured on a prefix of this capture's own records, so an agent
            # can decide before committing to a multi-gigabyte write.
            "estimated_bytes": _NULLABLE_INT,
            "bytes_per_record": _NULLABLE_NUMBER,
            "size_basis": _NULLABLE_STR,
        },
        ["format", "output", "records"],
        **{"$schema": _DRAFT, "$id": "ppk2lab:export-result"},
    ),
    "capture-manifest": _obj(
        {
            "format": {"const": "ppk2lab-capture"},
            "format_version": {"type": "integer"},
            "capture_id": {"type": "string"},
            "created_utc": {"type": "string"},
            "device": {"type": "object"},
            "configuration": {"type": "object"},
            "user_tags": _USER_TAGS,
            "scheduled_actions": _SCHEDULED_ACTIONS,
            "timeline": _obj(
                {
                    "sample_rate_hz": {"type": "integer", "minimum": 1},
                    "sample_period_ns": {"type": "integer"},
                    "start_index": {"type": "integer"},
                    "degraded": {"type": "boolean"},
                    "started_utc": {"type": "string"},
                    "ended_utc": {"type": "string"},
                    "wall_elapsed_s": _NULLABLE_NUMBER,
                    "timeline_advance": {"type": "integer"},
                    "achieved_sample_rate_hz": _NULLABLE_NUMBER,
                    "rate_deficit_ratio": _NULLABLE_NUMBER,
                    "rate_offset_ratio": _NULLABLE_NUMBER,
                    "first_sample_utc": _NULLABLE_STR,
                    "anchor_uncertainty_s": _NULLABLE_NUMBER,
                    "unaccounted_samples_estimate": _NULLABLE_INT,
                    "unaccounted_floor_samples": _NULLABLE_INT,
                    "rate_check": {"type": "string"},
                },
                ["sample_rate_hz", "sample_period_ns", "start_index"],
            ),
            "calibration": {
                "anyOf": [
                    {"type": "null"},
                    _obj(
                        {
                            "calibrated_flag": _NULLABLE_BOOL,
                            "metadata_terminated": _NULLABLE_BOOL,
                            "metadata_warnings": _STR_ARRAY,
                            "missing_ranges": {"type": ["array", "null"]},
                            "user_gains": {"type": ["array", "null"]},
                        }
                    ),
                ]
            },
            "samples": _obj(
                {
                    "encoding": {"const": "u32le-v1"},
                    "stored_count": {"type": "integer"},
                    "invalid_range_count": {"type": "integer"},
                    "sha256": {"type": "string"},
                    "chunks": {"type": "array"},
                }
            ),
            "gaps": {"type": "array", "items": _GAP},
            # Gaps the capture counted but did not enumerate; the count stays
            # exact even when the table stops growing.
            "gaps_truncated": {"type": "integer", "minimum": 0},
            "complete": {"type": "boolean"},
            "interruption": _NULLABLE_INTERRUPTION,
            "stats": {"type": ["object", "null"]},
            "warnings": {"type": "array"},
        },
        [
            "format",
            "format_version",
            "capture_id",
            "device",
            "configuration",
            "timeline",
            "samples",
            "gaps",
            "complete",
        ],
        **{"$schema": _DRAFT, "$id": "ppk2lab:capture-manifest"},
    ),
}


def list_schemas() -> list[str]:
    return sorted(SCHEMAS)


def get_schema(name: str) -> dict[str, Any]:
    try:
        return SCHEMAS[name]
    except KeyError as exc:
        raise SchemaNotFoundError(
            f"unknown schema {name!r}; available: {', '.join(list_schemas())}"
        ) from exc
