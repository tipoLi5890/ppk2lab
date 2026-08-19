"""JSON Lines export of the calibrated sample stream.

One JSON object per stored sample; gap records appear in-line so consumers
cannot miss data loss while streaming the file.
"""

from __future__ import annotations

import json
import math
import os

from ..capture.model import Capture
from ..errors import CaptureFileError, OutputExistsError
from ..protocol.samples import GapEvent


def export_samples_jsonl(
    capture: Capture,
    path: str | os.PathLike[str],
    *,
    overwrite: bool = False,
) -> int:
    """Write samples (and gap records) as JSONL; returns record count."""
    if os.path.exists(path) and not overwrite:
        raise OutputExistsError(f"output file exists: {path}")
    calibration = capture.calibration
    vdd = capture.source_voltage_mv
    records = 0
    samples = 0
    with open(path, "w", encoding="utf-8") as fh:
        for event in capture.iter_events():
            if isinstance(event, GapEvent):
                fh.write(json.dumps({"gap": event.to_json()}, sort_keys=True) + "\n")
                records += 1
                continue
            currents = calibration.convert_block(event, vdd) if calibration is not None else None
            ranges = event.ranges
            logic = event.logic
            for offset in range(len(event)):
                current = currents[offset] if currents is not None else None
                if current is not None and math.isnan(current):
                    current = None
                index = event.start_index + offset
                fh.write(
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
    if samples != capture.stored_count:
        raise CaptureFileError(
            f"JSONL export wrote {samples} sample records for a capture holding "
            f"{capture.stored_count} samples; refusing to report a partial export as success"
        )
    return records
