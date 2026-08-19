"""CSV export of calibrated samples.

Each row is one stored sample. ``current_ua`` is always the raw calibrated
value; the optional ``current_filtered_ua`` column adds the range-switch
smoothed series without ever replacing the raw one. ``gap_before_missing``
is non-empty on the first sample after a gap (``unknown`` when the gap size
could not be determined), so time is never silently compressed.
"""

from __future__ import annotations

import csv as _csv
import math
import os

from ..calibration import SpikeFilter
from ..capture.model import Capture
from ..errors import OutputExistsError
from ..protocol.samples import GapEvent


def export_csv(
    capture: Capture,
    path: str | os.PathLike[str],
    *,
    include_filtered: bool = False,
    overwrite: bool = False,
) -> int:
    """Write the capture as CSV; returns the number of data rows."""
    if os.path.exists(path) and not overwrite:
        raise OutputExistsError(f"output file exists: {path}")
    calibration = capture.calibration
    vdd = capture.source_voltage_mv
    spike = SpikeFilter() if include_filtered else None

    header = ["timeline_index", "time_s", "current_ua"]
    if include_filtered:
        header.append("current_filtered_ua")
    header += ["range", "counter", *[f"d{i}" for i in range(8)], "valid", "gap_before_missing"]

    rows = 0
    pending_gap: str = ""
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = _csv.writer(fh)
        writer.writerow(header)
        for event in capture.iter_events():
            if isinstance(event, GapEvent):
                pending_gap = str(event.missing) if event.missing is not None else "unknown"
                if spike:
                    spike.notify_gap()
                continue
            currents = calibration.convert_block(event, vdd) if calibration is not None else None
            filtered = (
                spike.apply(currents, event.ranges) if spike and currents is not None else None
            )
            ranges = event.ranges
            counters = event.counters
            logic = event.logic
            for offset in range(len(event)):
                index = event.start_index + offset
                current = currents[offset] if currents is not None else math.nan
                row: list[object] = [
                    index,
                    f"{capture.index_to_time(index):.6f}",
                    "" if math.isnan(current) else f"{current:.6f}",
                ]
                if filtered is not None:
                    value = filtered[offset]
                    row.append("" if math.isnan(value) else f"{value:.6f}")
                byte = logic[offset]
                row += [
                    ranges[offset],
                    counters[offset],
                    *[(byte >> bit) & 1 for bit in range(8)],
                    int(ranges[offset] <= 4),
                    pending_gap,
                ]
                pending_gap = ""
                writer.writerow(row)
                rows += 1
    return rows
