"""Measurement of windows and decoded events.

Every result carries the exact sample window it was computed from, plus gap
information, so conclusions always point back to raw evidence.
"""

from __future__ import annotations

import json
import os
from typing import Any

from ..capture.model import Capture
from ..capture.stats import WindowStats, compute_stats
from ..decoders.base import Annotation
from ..errors import CaptureFileError, UsageError


def measure_window(
    capture: Capture,
    *,
    start_s: float | None = None,
    end_s: float | None = None,
    start_index: int | None = None,
    end_index: int | None = None,
    filtered: bool = False,
    assume_voltage_mv: int | None = None,
    state_threshold_ua: float | None = None,
) -> WindowStats:
    """Statistics over a time or sample-index window of a capture."""
    if start_s is not None:
        start_index = capture.time_to_index(start_s)
    if end_s is not None:
        end_index = capture.time_to_index(end_s)
    return compute_stats(
        capture,
        start_index=start_index,
        end_index=end_index,
        filtered=filtered,
        assume_voltage_mv=assume_voltage_mv,
        state_threshold_ua=state_threshold_ua,
    )


def measure_annotations(
    capture: Capture,
    annotations: list[Annotation],
    *,
    kinds: list[str] | None = None,
    group_by: str = "annotation",
    filtered: bool = False,
    assume_voltage_mv: int | None = None,
) -> list[dict[str, Any]]:
    """Per-annotation or per-kind energy measurements.

    ``group_by='annotation'`` returns one entry per annotation with its own
    window statistics; ``group_by='kind'`` aggregates charge/energy per
    annotation kind.
    """
    selected = [a for a in annotations if kinds is None or a.kind in kinds]
    per_annotation: list[dict[str, Any]] = []
    for ann in selected:
        stats = compute_stats(
            capture,
            start_index=ann.start_sample,
            end_index=ann.end_sample,
            filtered=filtered,
            assume_voltage_mv=assume_voltage_mv,
        )
        per_annotation.append(
            {
                "annotation": ann.to_json(),
                "measurement": stats.to_json(),
            }
        )
    if group_by == "annotation":
        return per_annotation
    if group_by != "kind":
        raise UsageError(f"group_by must be 'annotation' or 'kind', got {group_by!r}")
    groups: dict[str, dict[str, Any]] = {}
    for entry in per_annotation:
        kind = entry["annotation"]["kind"]
        stats = entry["measurement"]
        group = groups.setdefault(
            kind,
            {
                "kind": kind,
                "count": 0,
                "total_charge_uc": 0.0,
                "total_energy_uj": 0.0,
                "energy_known": True,
                "total_duration_s": 0.0,
                "max_current_ua": None,
                "incomplete_windows": 0,
            },
        )
        group["count"] += 1
        group["total_duration_s"] += stats["duration_s"]
        charge = stats["charge_uc"]
        if charge is not None:
            group["total_charge_uc"] += charge
        energy = stats["energy_uj"]
        if energy is None:
            group["energy_known"] = False
        else:
            group["total_energy_uj"] += energy
        peak = stats["current_ua"]["max"]
        if peak is not None and (group["max_current_ua"] is None or peak > group["max_current_ua"]):
            group["max_current_ua"] = peak
        if not stats["complete"]:
            group["incomplete_windows"] += 1
    out = []
    for kind in sorted(groups):
        group = groups[kind]
        if not group["energy_known"]:
            group["total_energy_uj"] = None
        del group["energy_known"]
        out.append(group)
    return out


def save_annotations_jsonl(annotations: list[Annotation], path: str | os.PathLike[str]) -> int:
    """Write annotations as JSON Lines; returns the number written."""
    with open(path, "w", encoding="utf-8") as fh:
        for ann in annotations:
            fh.write(json.dumps(ann.to_json(), sort_keys=True) + "\n")
    return len(annotations)


def load_annotations_jsonl(path: str | os.PathLike[str]) -> list[Annotation]:
    annotations: list[Annotation] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    annotations.append(Annotation.from_json(json.loads(line)))
                # OverflowError joins the tuple because a JSON integer literal
                # has no width limit: an int too large for a C double raises it
                # rather than ValueError when a field is coerced to float.
                except (
                    json.JSONDecodeError,
                    KeyError,
                    OverflowError,
                    TypeError,
                    ValueError,
                ) as exc:
                    raise CaptureFileError(
                        f"invalid annotation on line {line_no} of {path}: {exc}"
                    ) from exc
    except FileNotFoundError as exc:
        raise CaptureFileError(f"annotation file not found: {path}") from exc
    return annotations
