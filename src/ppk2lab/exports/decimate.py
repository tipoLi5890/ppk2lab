"""Decimated export views: fixed-width timeline buckets, opt-in only.

A PPK2 records at a fixed 100 kS/s, so an hour is 360 million samples and a
raw CSV of it is tens of gigabytes. This module summarises a capture into
buckets small enough to plot — and does so **only when asked**. Nothing here
is reachable from a plain ``export``: an export whose output shape depended on
its input size would be derived data quietly standing in for raw data, and
the raw per-sample rows would stop being what the same command produces.

The record and every guarantee it carries are frozen in ``docs/decimation.md``
and were written down before this file was. Two of them shape the code:

- ``mean_ua`` is *defined* as ``charge_uc / (samples_in_bucket * 10 us)`` over
  the present samples, so merging buckets and re-deriving the mean is exact
  and re-decimating a decimated series cannot compound its own rounding;
- buckets span **timeline** positions, not stored samples, so a gap consumes
  bucket space instead of sliding later buckets earlier, and every bucket
  reports how many of its positions held nothing.

Min and max are what make the view trustworthy: a per-bucket mean is a
low-pass filter, and a decimated plot that lost the peak is the thing an
engineer would most regret trusting.
"""

from __future__ import annotations

import csv as _csv
import json
import math
import os
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from ..capture.model import Capture
from ..errors import CaptureFileError, UsageError
from ..protocol.samples import ADC_FULL_SCALE, ADC_MASK, MAX_VALID_RANGE, GapEvent, SampleBlock
from ..types import SAMPLE_PERIOD_S, SAMPLE_RATE_HZ
from ._atomic import atomic_write

#: The five auto-switching shunt ranges (same partition ``capture/stats.py``
#: reports); duplicated as a local so the hot loop does not import through it.
RANGE_COUNT = MAX_VALID_RANGE + 1

#: Currents past this are not reachable through the PPK2 shunts, so a larger
#: value means the 4-byte framing lost sync rather than that the DUT drew it.
#: Kept identical to ``capture.stats.IMPLAUSIBLE_CURRENT_UA``: a sample the
#: window statistics exclude must not be averaged into a bucket.
IMPLAUSIBLE_CURRENT_UA = 1_100_000.0

#: Frozen by ``docs/decimation.md``. No name is shared with the raw CSV export
#: header, so the two files are distinguishable from their first line alone.
DECIMATED_CSV_HEADER: tuple[str, ...] = (
    "bucket_start_index",
    "t_start_s",
    "bucket_samples",
    "samples_in_bucket",
    "missing_in_bucket",
    "unknown_gaps_in_bucket",
    "excluded_in_bucket",
    "saturated_in_bucket",
    "complete",
    "mean_ua",
    "min_ua",
    "max_ua",
    "charge_uc",
    "range0",
    "range1",
    "range2",
    "range3",
    "range4",
    "range_switches",
)

__all__ = [
    "DECIMATED_CSV_HEADER",
    "Bucket",
    "bucket_count",
    "bucket_samples_for_ms",
    "export_decimated_csv",
    "export_decimated_jsonl",
    "iter_buckets",
]


def bucket_samples_for_ms(bucket_ms: float, *, sample_rate_hz: int = SAMPLE_RATE_HZ) -> int:
    """Bucket width in samples for a width in milliseconds.

    Resolved against the capture's own recorded rate rather than a compiled-in
    constant: the rate is not configurable, but a stored artifact is the
    authority on what it was recorded at.
    """
    if not math.isfinite(bucket_ms) or bucket_ms <= 0:
        raise UsageError(
            f"bucket width must be a positive number of milliseconds, got {bucket_ms!r}"
        )
    width = round(bucket_ms * sample_rate_hz / 1000)
    if width < 1:
        raise UsageError(
            f"a {bucket_ms:g} ms bucket is shorter than one sample at {sample_rate_hz} Hz",
            remediation=(
                f"Use at least {1000 / sample_rate_hz:g} ms, or --decimate 1 for no "
                "decimation at all."
            ),
        )
    return width


@dataclass(frozen=True)
class Bucket:
    """One decimated record. See ``docs/decimation.md`` for the contract."""

    bucket_start_index: int
    t_start_s: float
    bucket_samples: int
    samples_in_bucket: int
    missing_in_bucket: int
    unknown_gaps_in_bucket: int
    excluded_in_bucket: int
    saturated_in_bucket: int
    mean_ua: float | None
    min_ua: float | None
    max_ua: float | None
    charge_uc: float | None
    range_occupancy: tuple[int, ...]
    range_switches: int

    @property
    def complete(self) -> bool:
        """True when every position in the bucket carried a usable sample.

        Derived rather than stored so it cannot disagree with the counts it
        summarises — the whole point of the field is that a bucket sitting
        over a gap can never pass for a full one.
        """
        return (
            self.missing_in_bucket == 0
            and self.unknown_gaps_in_bucket == 0
            and self.excluded_in_bucket == 0
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "bucket_start_index": self.bucket_start_index,
            "t_start_s": self.t_start_s,
            "bucket_samples": self.bucket_samples,
            "samples_in_bucket": self.samples_in_bucket,
            "missing_in_bucket": self.missing_in_bucket,
            "unknown_gaps_in_bucket": self.unknown_gaps_in_bucket,
            "excluded_in_bucket": self.excluded_in_bucket,
            "saturated_in_bucket": self.saturated_in_bucket,
            "complete": self.complete,
            "mean_ua": self.mean_ua,
            "min_ua": self.min_ua,
            "max_ua": self.max_ua,
            "charge_uc": self.charge_uc,
            "range_occupancy": list(self.range_occupancy),
            "range_switches": self.range_switches,
        }

    def to_row(self) -> list[object]:
        """CSV cells, in ``DECIMATED_CSV_HEADER`` order.

        Numbers go out at full float64 round-trip precision instead of a fixed
        number of decimals, so the ``mean_ua == charge_uc / (n * 10 us)``
        identity is checkable from the file. The raw export rounds because it
        is meant to be read; this file is meant to be recombined.
        """

        def cell(value: float | None) -> object:
            return "" if value is None else repr(value)

        return [
            self.bucket_start_index,
            repr(self.t_start_s),
            self.bucket_samples,
            self.samples_in_bucket,
            self.missing_in_bucket,
            self.unknown_gaps_in_bucket,
            self.excluded_in_bucket,
            self.saturated_in_bucket,
            int(self.complete),
            cell(self.mean_ua),
            cell(self.min_ua),
            cell(self.max_ua),
            cell(self.charge_uc),
            *self.range_occupancy,
            self.range_switches,
        ]


class _BucketAccumulator:
    """Running totals for one bucket. Reset in place rather than reallocated."""

    __slots__ = (
        "_compensation",
        "excluded",
        "max_v",
        "min_v",
        "missing",
        "range_occupancy",
        "range_switches",
        "samples",
        "saturated",
        "total",
        "unknown_gaps",
    )

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.samples = 0
        self.missing = 0
        self.unknown_gaps = 0
        self.excluded = 0
        self.saturated = 0
        # Neumaier-compensated, for the same reason capture/stats.py is: a
        # bucket may be minutes wide, and once the running total is large
        # enough a sub-microamp sleep sample is below its ULP and stops
        # contributing at all.
        self.total = 0.0
        self._compensation = 0.0
        self.min_v: float | None = None
        self.max_v: float | None = None
        self.range_occupancy = [0] * RANGE_COUNT
        self.range_switches = 0

    def add(self, value: float) -> None:
        running = self.total + value
        if abs(self.total) >= abs(value):
            self._compensation += (self.total - running) + value
        else:
            self._compensation += (value - running) + self.total
        self.total = running
        if self.min_v is None or value < self.min_v:
            self.min_v = value
        if self.max_v is None or value > self.max_v:
            self.max_v = value

    def finish(self, *, start: int, width: int, t_start_s: float) -> Bucket:
        charge: float | None = None
        mean: float | None = None
        if self.samples:
            charge = (self.total + self._compensation) * SAMPLE_PERIOD_S
            # The definition, not a second independent statistic: see
            # docs/decimation.md. Merging buckets and re-deriving the mean this
            # way reproduces the mean over the merged span exactly.
            mean = charge / (self.samples * SAMPLE_PERIOD_S)
        return Bucket(
            bucket_start_index=start,
            t_start_s=t_start_s,
            bucket_samples=width,
            samples_in_bucket=self.samples,
            missing_in_bucket=self.missing,
            unknown_gaps_in_bucket=self.unknown_gaps,
            excluded_in_bucket=self.excluded,
            saturated_in_bucket=self.saturated,
            mean_ua=mean,
            min_ua=self.min_v,
            max_ua=self.max_v,
            charge_uc=charge,
            range_occupancy=tuple(self.range_occupancy),
            range_switches=self.range_switches,
        )


def iter_buckets(
    capture: Capture,
    *,
    bucket_samples: int,
) -> Iterator[Bucket]:
    """Summarise a capture into fixed-width timeline buckets.

    Buckets start at the capture's first timeline position and step by
    ``bucket_samples``; the last one is truncated at the end of the capture
    rather than padded, so positions the instrument never reached are not
    reported as loss.
    """
    if bucket_samples < 1:
        raise UsageError(f"bucket width must be at least one sample, got {bucket_samples}")
    calibration = capture.calibration
    vdd = capture.source_voltage_mv
    origin = capture.start_index
    end = capture.end_index

    acc = _BucketAccumulator()
    bucket_start = origin
    bucket_end = min(bucket_start + bucket_samples, end)
    # A range change is attributed to the bucket holding the later of the two
    # adjacent samples, including across a bucket boundary; that is what makes
    # the per-bucket counts sum to the whole-window figure `measure` reports.
    last_range: int | None = None

    def flush() -> Bucket:
        return acc.finish(
            start=bucket_start,
            width=bucket_end - bucket_start,
            t_start_s=capture.index_to_time(bucket_start),
        )

    for event in capture.iter_events():
        if isinstance(event, GapEvent):
            # Two samples separated by missing data are not adjacent, so the
            # next range difference is not an observed switch.
            last_range = None
            position = event.index
            while position >= bucket_end and bucket_end < end:
                yield flush()
                acc.reset()
                bucket_start = bucket_end
                bucket_end = min(bucket_start + bucket_samples, end)
            if event.missing is None:
                # A gap of undetermined size occupies no timeline width, so it
                # consumes no bucket positions — but data was still lost here,
                # and a bucket that reported `complete` over one would be
                # claiming a span it cannot account for.
                acc.unknown_gaps += 1
                continue
            stop = position + event.missing
            while position < stop:
                while position >= bucket_end and bucket_end < end:
                    yield flush()
                    acc.reset()
                    bucket_start = bucket_end
                    bucket_end = min(bucket_start + bucket_samples, end)
                take = min(stop, bucket_end) - position
                if take <= 0:
                    break
                acc.missing += take
                position += take
            continue

        block: SampleBlock = event
        currents = calibration.convert_block(block, vdd) if calibration is not None else None
        ranges = block.ranges
        words = block.words
        index = block.start_index
        for offset in range(len(block)):
            while index >= bucket_end and bucket_end < end:
                yield flush()
                acc.reset()
                bucket_start = bucket_end
                bucket_end = min(bucket_start + bucket_samples, end)
            index += 1
            range_index = ranges[offset]
            value = currents[offset] if currents is not None else math.nan
            if (
                range_index > MAX_VALID_RANGE
                or math.isnan(value)
                or value > IMPLAUSIBLE_CURRENT_UA
                or value < -IMPLAUSIBLE_CURRENT_UA
            ):
                # Delivered but unusable: counted apart from missing data
                # because the two are different failures, and folding it into
                # the present count would dilute a mean it carries no charge
                # towards.
                acc.excluded += 1
                continue
            acc.samples += 1
            acc.range_occupancy[range_index] += 1
            if range_index != last_range:
                if last_range is not None:
                    acc.range_switches += 1
                last_range = range_index
            if words[offset] & ADC_MASK == ADC_FULL_SCALE:
                # Pinned, not measured: without this counter a clipped burst
                # renders as a clean flat top and `max_ua` reads as a peak.
                acc.saturated += 1
            acc.add(value)

    if bucket_end > bucket_start:
        yield flush()


def bucket_count(capture: Capture, bucket_samples: int) -> int:
    """How many records an export of this capture will write.

    Follows from the timeline span alone, so it is knowable before a single
    sample is converted — which is what lets ``export --json`` estimate the
    size of a decimated write before committing to it.
    """
    span = capture.end_index - capture.start_index
    if span <= 0:
        return 0
    return -(-span // bucket_samples)


def write_decimated_csv(handle: Any, buckets: Iterator[Bucket]) -> int:
    """Write the decimated CSV body to an open handle; returns bucket count."""
    writer = _csv.writer(handle)
    writer.writerow(DECIMATED_CSV_HEADER)
    count = 0
    for bucket in buckets:
        writer.writerow(bucket.to_row())
        count += 1
    return count


def write_decimated_jsonl(handle: Any, buckets: Iterator[Bucket]) -> int:
    """Write the decimated JSONL body to an open handle; returns bucket count."""
    count = 0
    for bucket in buckets:
        # Nested under one key, mirroring the raw export's {"gap": ...}: a
        # consumer that looks at a line's single top-level key always knows
        # what it is holding, and no raw sample record can parse as a bucket.
        handle.write(json.dumps({"bucket": bucket.to_json()}, sort_keys=True) + "\n")
        count += 1
    return count


def export_decimated_csv(
    capture: Capture,
    path: str | os.PathLike[str],
    *,
    bucket_samples: int,
    overwrite: bool = False,
) -> int:
    """Write the decimated view as CSV; returns the number of buckets."""
    expected = bucket_count(capture, bucket_samples)
    with atomic_write(path, overwrite=overwrite, newline="", encoding="utf-8") as fh:
        count = write_decimated_csv(fh, iter_buckets(capture, bucket_samples=bucket_samples))
        _check_count(count, expected)
    return count


def export_decimated_jsonl(
    capture: Capture,
    path: str | os.PathLike[str],
    *,
    bucket_samples: int,
    overwrite: bool = False,
) -> int:
    """Write the decimated view as JSONL; returns the number of buckets."""
    expected = bucket_count(capture, bucket_samples)
    with atomic_write(path, overwrite=overwrite, encoding="utf-8") as fh:
        count = write_decimated_jsonl(fh, iter_buckets(capture, bucket_samples=bucket_samples))
        _check_count(count, expected)
    return count


def _check_count(written: int, expected: int) -> None:
    """Refuse a short export, inside the atomic block so it never lands.

    The bucket count follows from the capture's timeline span alone, so a
    mismatch means the walk and the timeline disagree — which would show up in
    the file as a silently shortened time axis.
    """
    if written != expected:
        raise CaptureFileError(
            f"decimated export wrote {written} buckets for a timeline that spans "
            f"{expected}; refusing to report a partial export as success"
        )
