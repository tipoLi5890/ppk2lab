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
            }
        ),
        "current_ua": _obj(
            {
                "mean": _NULLABLE_NUMBER,
                "min": _NULLABLE_NUMBER,
                "max": _NULLABLE_NUMBER,
                "peak_index": _NULLABLE_INT,
            }
        ),
        "charge_uc": _NULLABLE_NUMBER,
        "energy_uj": _NULLABLE_NUMBER,
        "source_voltage_mv": _NULLABLE_INT,
        "complete": {"type": "boolean"},
        "sample_gaps": {"type": "array", "items": _GAP},
    },
    ["window", "duration_s", "samples", "current_ua", "complete", "sample_gaps"],
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

SCHEMAS: dict[str, dict[str, Any]] = {
    "envelope": _obj(
        {
            "schema_version": {"const": SCHEMA_VERSION},
            "command": {"type": "string"},
            "ok": {"type": "boolean"},
            "result": {"type": ["object", "null"]},
            "warnings": _STR_ARRAY,
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
            "schemas": _STR_ARRAY,
        },
        ["device", "commands", "decoders", "exit_codes", "error_codes", "schemas"],
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
    "capture-result": _obj(
        {
            "capture_id": {"type": "string"},
            "capture_sha256": _NULLABLE_STR,
            "path": _NULLABLE_STR,
            "complete": {"type": "boolean"},
            "interruption": {"type": ["object", "null"]},
            "trigger": {"type": ["object", "null"]},
            "stats": {"anyOf": [{"type": "null"}, _WINDOW_STATS]},
            "warnings": _STR_ARRAY,
        },
        ["capture_id", "complete", "stats", "warnings"],
        **{"$schema": _DRAFT, "$id": "ppk2lab:capture-result"},
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
    "export-result": _obj(
        {
            "format": {"enum": ["csv", "vcd", "jsonl"]},
            "output": {"type": "string"},
            "records": {"type": "integer"},
            "capture_id": {"type": "string"},
            "capture_sha256": {"type": "string"},
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
            "timeline": _obj(
                {
                    "sample_rate_hz": {"type": "integer"},
                    "sample_period_ns": {"type": "integer"},
                    "start_index": {"type": "integer"},
                    "degraded": {"type": "boolean"},
                }
            ),
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
            "complete": {"type": "boolean"},
            "interruption": {"type": ["object", "null"]},
            "stats": {"type": ["object", "null"]},
            "warnings": _STR_ARRAY,
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
