"""JSON Lines export of the calibrated sample stream.

One JSON object per stored sample; gap records appear in-line so consumers
cannot miss data loss while streaming the file. Sample records are flat
objects carrying an ``index``; gap records nest under a ``gap`` key. Decimated
buckets are a separate, opt-in export and nest under ``bucket``
(``exports/decimate.py``), so no line of one file parses as a line of the
other.

The file is written to a temp path and moved into place, so a failed export
never replaces a previous one.
"""

from __future__ import annotations

import json
import math
import os
from typing import Any

from ..capture.model import Capture
from ..errors import CaptureFileError
from ..protocol.samples import GapEvent
from ._atomic import atomic_write


def write_samples_jsonl(
    capture: Capture,
    handle: Any,
    *,
    max_records: int | None = None,
) -> tuple[int, int]:
    """Write records to an open handle; returns ``(records, sample_records)``.

    ``max_records`` stops early and exists for the size estimator, which
    renders a prefix through this exact formatter rather than guessing a record
    width from a compiled-in average.
    """
    calibration = capture.calibration
    vdd = capture.source_voltage_mv
    records = 0
    samples = 0
    for event in capture.iter_events():
        if max_records is not None and records >= max_records:
            return records, samples
        if isinstance(event, GapEvent):
            handle.write(json.dumps({"gap": event.to_json()}, sort_keys=True) + "\n")
            records += 1
            continue
        currents = calibration.convert_block(event, vdd) if calibration is not None else None
        ranges = event.ranges
        logic = event.logic
        for offset in range(len(event)):
            if max_records is not None and records >= max_records:
                return records, samples
            current = currents[offset] if currents is not None else None
            if current is not None and math.isnan(current):
                current = None
            index = event.start_index + offset
            handle.write(
                json.dumps(
                    {
                        "index": index,
                        "time_s": round(capture.index_to_time(index), 6),
                        "current_ua": current,
                        "range": ranges[offset],
                        "logic": logic[offset],
                        "valid": ranges[offset] <= 4,
                    },
                    sort_keys=True,
                )
                + "\n"
            )
            records += 1
            samples += 1
    return records, samples


def export_samples_jsonl(
    capture: Capture,
    path: str | os.PathLike[str],
    *,
    overwrite: bool = False,
) -> int:
    """Write samples (and gap records) as JSONL; returns record count."""
    with atomic_write(path, overwrite=overwrite, encoding="utf-8") as fh:
        records, samples = write_samples_jsonl(capture, fh)
        if samples != capture.stored_count:
            # Raised inside the block on purpose: aborting here leaves any
            # previous export in place instead of replacing it with a partial
            # file that looks complete next to an error the caller may not read.
            raise CaptureFileError(
                f"JSONL export wrote {samples} sample records for a capture holding "
                f"{capture.stored_count} samples; refusing to report a partial export as success"
            )
    return records
