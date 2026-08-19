"""Derived exports (CSV, JSONL, VCD). Raw captures remain the evidence;
exports are reproducible views and never replace the artifact.

Every exporter writes through :mod:`ppk2lab.exports._atomic`, so an export
that fails part-way leaves the destination — often the previous good export —
exactly as it was.

Two shapes exist and they never share a command's default: the raw exporters
emit one record per stored sample, whatever the capture's length, and the
decimated exporters in :mod:`ppk2lab.exports.decimate` emit one record per
timeline bucket only when a bucket width is asked for. Their headers and
record shapes are disjoint by design (``docs/decimation.md``).
"""

from __future__ import annotations

import io
from itertools import islice
from typing import Any

from ..analysis.measure import load_annotations_jsonl, save_annotations_jsonl
from ..capture.model import Capture
from ..logic.vcd import export_vcd
from .csv import csv_header, export_csv, write_csv
from .decimate import (
    DECIMATED_CSV_HEADER,
    Bucket,
    bucket_count,
    bucket_samples_for_ms,
    export_decimated_csv,
    export_decimated_jsonl,
    iter_buckets,
    write_decimated_csv,
    write_decimated_jsonl,
)
from .jsonl import export_samples_jsonl, write_samples_jsonl

__all__ = [
    "DECIMATED_CSV_HEADER",
    "Bucket",
    "bucket_count",
    "bucket_samples_for_ms",
    "csv_header",
    "estimate_export_size",
    "export_csv",
    "export_decimated_csv",
    "export_decimated_jsonl",
    "export_samples_jsonl",
    "export_vcd",
    "iter_buckets",
    "load_annotations_jsonl",
    "save_annotations_jsonl",
]

#: How many records the estimator renders before extrapolating. Large enough
#: that one unusually wide row does not dominate, small enough that estimating
#: an eight-hour export costs milliseconds.
PROBE_RECORDS = 2000
#: Decimated records are far fewer, so the probe is counted in buckets.
PROBE_BUCKETS = 200


def estimate_export_size(
    capture: Capture,
    *,
    export_format: str,
    include_filtered: bool = False,
    bucket_samples: int | None = None,
) -> dict[str, Any]:
    """Estimate the size of an export before committing to writing it.

    One hour of capture is roughly 19 GB of raw CSV and 40 GB of raw JSONL, so
    "how big will this be" is a question worth answering before the write
    starts rather than after the disk fills.

    ``bytes_per_record`` is *measured*, by rendering a prefix of this capture's
    own records through the exporter that will write the file — not read from a
    table of averages. Record width depends on the values (a 6 uA sleep sample
    is narrower than a 12000.123456 uA burst), so a compiled-in constant would
    be a guess presented as a fact. It remains an estimate, and the ``basis``
    string says so.
    """
    decimated = bucket_samples is not None
    result: dict[str, Any] = {
        "format": export_format,
        "decimated": decimated,
        "records": None,
        "bytes_per_record": None,
        "estimated_bytes": None,
        "basis": None,
    }
    if export_format == "vcd":
        # A VCD carries one record per logic-line change, and how often D0-D7
        # change is a property of the DUT that nothing short of walking the
        # capture can predict. Reporting null beats reporting a number derived
        # from an assumption about someone else's firmware.
        result["basis"] = "not estimated: VCD record count depends on the logic activity"
        return result
    if export_format not in ("csv", "jsonl"):
        result["basis"] = f"not estimated: unknown export format {export_format!r}"
        return result

    if decimated:
        assert bucket_samples is not None
        records = bucket_count(capture, bucket_samples)
        probe = min(records, PROBE_BUCKETS)
        header_bytes, body_bytes, probed = _probe_decimated(
            capture, export_format, bucket_samples, probe
        )
        basis = f"measured over the first {probed} bucket(s) of this capture"
    elif export_format == "csv":
        records = capture.stored_count
        header = ",".join(csv_header(include_filtered=include_filtered)) + "\r\n"
        header_bytes = len(header.encode("utf-8"))
        buf = io.StringIO(newline="")
        probed = write_csv(
            capture, buf, include_filtered=include_filtered, max_rows=min(records, PROBE_RECORDS)
        )
        body_bytes = len(buf.getvalue().encode("utf-8")) - header_bytes
        basis = (
            f"measured over the first {probed} row(s) of this capture; gap marker rows "
            "are not included"
        )
    else:
        records = capture.stored_count + len(capture.gaps)
        header_bytes = 0
        buf = io.StringIO()
        probed, _ = write_samples_jsonl(capture, buf, max_records=min(records, PROBE_RECORDS))
        body_bytes = len(buf.getvalue().encode("utf-8"))
        basis = f"measured over the first {probed} record(s) of this capture"

    result["records"] = records
    if probed <= 0:
        result["estimated_bytes"] = header_bytes
        result["basis"] = "no records to measure; only the header will be written"
        return result
    per_record = body_bytes / probed
    result["bytes_per_record"] = per_record
    result["estimated_bytes"] = round(header_bytes + per_record * records)
    result["basis"] = basis
    return result


def _probe_decimated(
    capture: Capture, export_format: str, bucket_samples: int, probe: int
) -> tuple[int, int, int]:
    """Render ``probe`` buckets and report ``(header, body, rendered)`` bytes."""
    buckets = islice(iter_buckets(capture, bucket_samples=bucket_samples), probe)
    if export_format == "csv":
        header_bytes = len((",".join(DECIMATED_CSV_HEADER) + "\r\n").encode("utf-8"))
        buf = io.StringIO(newline="")
        rendered = write_decimated_csv(buf, buckets)
        return header_bytes, len(buf.getvalue().encode("utf-8")) - header_bytes, rendered
    buf = io.StringIO()
    rendered = write_decimated_jsonl(buf, buckets)
    return 0, len(buf.getvalue().encode("utf-8")), rendered
