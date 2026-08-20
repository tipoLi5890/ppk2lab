"""CSV export of calibrated samples.

Each row is one stored sample. ``current_ua`` is always the raw calibrated
value; the optional ``current_filtered_ua`` column adds the range-switch
smoothed series without ever replacing the raw one. ``gap_before_missing``
is non-empty on the first sample after a gap (``unknown`` when the gap size
could not be determined), so time is never silently compressed. A gap with no
stored sample after it gets a row of its own rather than disappearing.

One row per sample, always: the header does not change with the size of the
capture, and there is no input for which this exporter emits a summary
instead. Decimated views are a separate, opt-in export with a header of their
own (``exports/decimate.py``, ``docs/decimation.md``).

The file is written to a temp path and moved into place, so a failed export
never replaces a previous one.
"""

from __future__ import annotations

import csv as _csv
import math
import os
from collections.abc import Sequence
from typing import Any

from ..calibration import SpikeFilter
from ..capture.model import Capture
from ..errors import CaptureFileError
from ..protocol.samples import GapEvent
from ._atomic import atomic_write, write_comments


def csv_header(*, include_filtered: bool = False) -> list[str]:
    """The raw export's column names.

    Frozen: a golden test pins this list byte-for-byte, because the guarantee
    that a decimated file can never be mistaken for a raw one rests on the two
    headers sharing no column name.
    """
    header = ["timeline_index", "time_s", "current_ua"]
    if include_filtered:
        header.append("current_filtered_ua")
    header += ["range", "counter", *[f"d{i}" for i in range(8)], "valid", "gap_before_missing"]
    return header


def write_csv(
    capture: Capture,
    handle: Any,
    *,
    include_filtered: bool = False,
    max_rows: int | None = None,
) -> int:
    """Write header and rows to an open handle; returns the sample-row count.

    ``max_rows`` stops after that many sample rows and exists for the size
    estimator, which renders a prefix through this exact formatter rather than
    guessing a row width from a compiled-in average.
    """
    calibration = capture.calibration
    vdd = capture.source_voltage_mv
    spike = SpikeFilter() if include_filtered else None

    def gap_cell(gap: GapEvent) -> str:
        return str(gap.missing) if gap.missing is not None else "unknown"

    def gap_row(gap: GapEvent) -> list[object]:
        row: list[object] = [gap.index, f"{capture.index_to_time(gap.index):.6f}", ""]
        if include_filtered:
            row.append("")
        # Range, counter, and D0-D7 are unknown for samples that never
        # arrived: left empty rather than filled with a fabricated zero.
        # valid=0 keeps the row out of any numeric filter.
        row += ["", "", *([""] * 8), 0, gap_cell(gap)]
        return row

    rows = 0
    pending: GapEvent | None = None
    pending_cell = ""
    writer = _csv.writer(handle)
    writer.writerow(csv_header(include_filtered=include_filtered))
    for event in capture.iter_events():
        if isinstance(event, GapEvent):
            if pending is not None:
                # Two gaps with nothing stored between them: the first has
                # no sample row left to carry its marker.
                writer.writerow(gap_row(pending))
            pending, pending_cell = event, gap_cell(event)
            if spike:
                spike.notify_gap()
            continue
        currents = calibration.convert_block(event, vdd) if calibration is not None else None
        filtered = spike.apply(currents, event.ranges) if spike and currents is not None else None
        ranges = event.ranges
        counters = event.counters
        logic = event.logic
        for offset in range(len(event)):
            if max_rows is not None and rows >= max_rows:
                return rows
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
                pending_cell,
            ]
            pending, pending_cell = None, ""
            writer.writerow(row)
            rows += 1
    if pending is not None:
        # A gap that ends the capture has no following row to carry it, and
        # dropping it would export a lossy capture as a contiguous one.
        writer.writerow(gap_row(pending))
    return rows


def export_csv(
    capture: Capture,
    path: str | os.PathLike[str],
    *,
    include_filtered: bool = False,
    overwrite: bool = False,
    comments: Sequence[str] | None = None,
) -> int:
    """Write the capture as CSV; returns the number of sample rows.

    Gap marker rows are not counted: the return value is the number of stored
    samples, so a consumer counting lines may see more rows than this.
    """
    with atomic_write(path, overwrite=overwrite, newline="", encoding="utf-8") as fh:
        write_comments(fh, comments)
        rows = write_csv(capture, fh, include_filtered=include_filtered)
        if rows != capture.stored_count:
            # Raised inside the block on purpose: aborting here leaves any
            # previous export in place instead of replacing it with a partial
            # file that looks complete next to an error the caller may not read.
            raise CaptureFileError(
                f"CSV export wrote {rows} rows for a capture holding "
                f"{capture.stored_count} samples; refusing to report a partial export as success"
            )
    return rows
