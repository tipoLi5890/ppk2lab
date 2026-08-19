"""Window/annotation measurement and CSV/JSONL/VCD exports."""

import csv
import json
from array import array

import pytest

from ppk2lab.analysis.measure import (
    load_annotations_jsonl,
    measure_annotations,
    measure_window,
    save_annotations_jsonl,
)
from ppk2lab.capture.model import Capture
from ppk2lab.capture.stats import compute_stats
from ppk2lab.decoders.base import Annotation
from ppk2lab.errors import CaptureFileError, OutputExistsError, UsageError
from ppk2lab.exports import (
    DECIMATED_CSV_HEADER,
    bucket_samples_for_ms,
    estimate_export_size,
    export_csv,
    export_decimated_csv,
    export_decimated_jsonl,
    export_samples_jsonl,
    iter_buckets,
)
from ppk2lab.protocol.samples import GapEvent, SampleBlock, pack_sample
from ppk2lab.testing.profiles import ConstantProfile, StepProfile

from .conftest import capture_of


@pytest.fixture
def capture():
    return capture_of(StepProfile([(500, 2000.0, 0x01), (1500, 10.0, 0x00)]), samples=2000)


def test_measure_window_by_time(capture):
    stats = measure_window(capture, start_s=0.0, end_s=0.005)
    assert stats.stored_samples == 500
    assert stats.mean_ua == pytest.approx(2000.0, rel=0.01)
    stats = measure_window(capture, start_s=0.005, end_s=0.02)
    assert stats.mean_ua == pytest.approx(10.0, rel=0.01)


def test_measure_annotations_per_event_and_kind(capture):
    annotations = [
        Annotation("uart", "frame", 0, 500, {"value": 65}),
        Annotation("uart", "frame", 500, 1000, {"value": 66}),
    ]
    per_event = measure_annotations(capture, annotations, group_by="annotation")
    assert len(per_event) == 2
    assert per_event[0]["measurement"]["current_ua"]["mean"] == pytest.approx(2000.0, rel=0.01)
    groups = measure_annotations(capture, annotations, group_by="kind")
    assert groups[0]["kind"] == "frame"
    assert groups[0]["count"] == 2
    # 500 samples @2000uA + 500 @10uA = 10.05 uC total
    assert groups[0]["total_charge_uc"] == pytest.approx(10.05, rel=0.01)


def test_annotations_jsonl_roundtrip(tmp_path):
    annotations = [
        Annotation("uart", "frame", 10, 114, {"value": 65, "byte": "0x41"}, 1.0, []),
        Annotation("uart", "error", 200, 250, {"reason": "sample_gap"}, 0.0, ["gap"]),
    ]
    path = tmp_path / "ann.jsonl"
    assert save_annotations_jsonl(annotations, path) == 2
    loaded = load_annotations_jsonl(path)
    assert [a.to_json() for a in loaded] == [a.to_json() for a in annotations]


def test_annotations_jsonl_malformed_rejected(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text('{"decoder": "uart"}\n')  # missing required keys
    with pytest.raises(CaptureFileError):
        load_annotations_jsonl(path)
    with pytest.raises(CaptureFileError):
        load_annotations_jsonl(tmp_path / "absent.jsonl")


def test_csv_export_columns_and_gap_marker(tmp_path):
    capture = capture_of(StepProfile([(100, 100.0, 0x03)], repeat=True), samples=200, gaps={50: 5})
    path = tmp_path / "out.csv"
    rows = export_csv(capture, path)
    with open(path) as fh:
        reader = csv.DictReader(fh)
        data = list(reader)
    assert rows == len(data) == 195  # 200 timeline - 5 missing
    assert data[0]["d0"] == "1" and data[0]["d1"] == "1" and data[0]["d2"] == "0"
    assert float(data[0]["current_ua"]) == pytest.approx(100.0, rel=0.01)
    # the first row after the gap carries the marker and the right index
    marked = [r for r in data if r["gap_before_missing"]]
    assert len(marked) == 1
    assert marked[0]["gap_before_missing"] == "5"
    assert marked[0]["timeline_index"] == "55"
    with pytest.raises(OutputExistsError):
        export_csv(capture, path)


def test_csv_filtered_column_keeps_raw(tmp_path):
    capture = capture_of(StepProfile([(50, 10.0, 0), (50, 40000.0, 0)]), samples=100)
    path = tmp_path / "f.csv"
    export_csv(capture, path, include_filtered=True)
    with open(path) as fh:
        header = fh.readline().strip().split(",")
    assert "current_ua" in header and "current_filtered_ua" in header


def test_jsonl_export_includes_gap_records(tmp_path):
    capture = capture_of(StepProfile([(100, 100.0, 0)], repeat=True), samples=120, gaps={60: 3})
    path = tmp_path / "out.jsonl"
    records = export_samples_jsonl(capture, path)
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert records == len(lines) == 117 + 1  # 117 samples + 1 gap record
    gap_lines = [line for line in lines if "gap" in line]
    assert gap_lines == [
        {"gap": {"index": 60, "missing": 3, "reason": "counter_skip", "ambiguous": True}}
    ]


# ---------------------------------------------------------------------------
# Gaps with no sample row after them
#


def _with_trailing_gap(capture, missing):
    """Same samples, plus a gap recorded past the last stored sample."""
    gap = GapEvent(capture.end_index, missing, "host_overflow")
    return Capture(capture.meta, capture.words, [*capture.gaps, gap])


def test_csv_records_a_gap_that_ends_the_capture(tmp_path):
    """A trailing gap gets a row of its own instead of vanishing."""
    capture = _with_trailing_gap(capture_of(StepProfile([(100, 100.0, 0)]), samples=100), 35)
    path = tmp_path / "trailing.csv"
    rows = export_csv(capture, path)
    with open(path) as fh:
        data = list(csv.DictReader(fh))
    # the return value still counts stored samples only
    assert rows == capture.stored_count == 100
    assert len(data) == 101
    marker = data[-1]
    assert marker["gap_before_missing"] == "35"
    assert marker["timeline_index"] == "100"
    assert marker["valid"] == "0"
    # nothing is invented for samples that never arrived
    assert marker["current_ua"] == "" and marker["range"] == "" and marker["counter"] == ""
    assert all(marker[f"d{bit}"] == "" for bit in range(8))
    # and the two exports of one artifact agree that data was lost
    jsonl_path = tmp_path / "trailing.jsonl"
    export_samples_jsonl(capture, jsonl_path)
    last = json.loads(jsonl_path.read_text().splitlines()[-1])
    assert last["gap"]["missing"] == 35


def test_csv_records_an_unknown_trailing_gap_as_unknown(tmp_path):
    """A trailing gap of undetermined size is marked, never assumed empty."""
    capture = _with_trailing_gap(capture_of(StepProfile([(100, 100.0, 0)]), samples=100), None)
    path = tmp_path / "desync.csv"
    export_csv(capture, path)
    with open(path) as fh:
        data = list(csv.DictReader(fh))
    assert data[-1]["gap_before_missing"] == "unknown"


def test_csv_records_back_to_back_gaps_separately(tmp_path):
    """Two gaps with nothing stored between them stay two distinct markers."""
    base = capture_of(StepProfile([(100, 100.0, 0)], repeat=True), samples=200)
    gaps = [GapEvent(100, 5, "counter_skip"), GapEvent(105, 3, "host_overflow")]
    capture = Capture(base.meta, base.words, gaps)
    path = tmp_path / "adjacent.csv"
    rows = export_csv(capture, path)
    with open(path) as fh:
        data = list(csv.DictReader(fh))
    assert rows == 200
    marked = [(r["timeline_index"], r["gap_before_missing"], r["valid"]) for r in data]
    marked = [m for m in marked if m[1]]
    # the first gap gets a marker row of its own; the second rides the next sample
    assert marked == [("100", "5", "0"), ("108", "3", "1")]


# ---------------------------------------------------------------------------
# Atomic replacement: a failed export never destroys the previous one
#


class _DiskFull(OSError):
    """Stands in for the ordinary large-export failure (ENOSPC)."""


def _fails_after(capture, stored_samples):
    """Make iteration die part-way, the way a full disk kills a long export."""

    def iter_events(block_samples=65536):
        for event in Capture.iter_events(capture, block_samples):
            if isinstance(event, SampleBlock) and len(event) > stored_samples:
                yield SampleBlock(event.start_index, event.words[:stored_samples])
                raise _DiskFull(28, "No space left on device")
            yield event

    capture.iter_events = iter_events
    return capture


def _truncates_after(capture, stored_samples):
    """Iteration that ends early without raising: the row-count check case."""

    def iter_events(block_samples=65536):
        for event in Capture.iter_events(capture, block_samples):
            if isinstance(event, SampleBlock) and len(event) > stored_samples:
                yield SampleBlock(event.start_index, event.words[:stored_samples])
                return
            yield event

    capture.iter_events = iter_events
    return capture


@pytest.fixture
def prior(tmp_path):
    """A previous good export sitting at the destination path."""
    path = tmp_path / "export.out"
    path.write_bytes(b"previous good export\n")
    return path


def test_csv_export_failure_leaves_the_previous_export_intact(tmp_path, prior):
    """A mid-write failure must not cost the user the file already there."""
    capture = _fails_after(capture_of(StepProfile([(2000, 100.0, 0)]), samples=2000), 500)
    with pytest.raises(_DiskFull):
        export_csv(capture, prior, overwrite=True)
    assert prior.read_bytes() == b"previous good export\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["export.out"]


def test_jsonl_export_failure_leaves_the_previous_export_intact(tmp_path, prior):
    """Same contract for JSONL, which previously had no cleanup at all."""
    capture = _fails_after(capture_of(StepProfile([(2000, 100.0, 0)]), samples=2000), 500)
    with pytest.raises(_DiskFull):
        export_samples_jsonl(capture, prior, overwrite=True)
    assert prior.read_bytes() == b"previous good export\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["export.out"]


def test_csv_row_count_check_aborts_before_replacing_the_target(tmp_path, prior):
    """The row-count refusal must abort the export, not clean up after it."""
    capture = _truncates_after(capture_of(StepProfile([(2000, 100.0, 0)]), samples=2000), 500)
    with pytest.raises(CaptureFileError):
        export_csv(capture, prior, overwrite=True)
    assert prior.read_bytes() == b"previous good export\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["export.out"]


def test_jsonl_record_count_check_aborts_before_replacing_the_target(tmp_path, prior):
    capture = _truncates_after(capture_of(StepProfile([(2000, 100.0, 0)]), samples=2000), 500)
    with pytest.raises(CaptureFileError):
        export_samples_jsonl(capture, prior, overwrite=True)
    assert prior.read_bytes() == b"previous good export\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["export.out"]


def test_successful_overwrite_replaces_the_target_and_counts_the_same(tmp_path, prior):
    """The atomic path must not change what a successful export reports."""
    capture = capture_of(StepProfile([(100, 100.0, 0)], repeat=True), samples=200, gaps={50: 5})
    fresh = tmp_path / "fresh.csv"
    assert export_csv(capture, fresh) == export_csv(capture, prior, overwrite=True) == 195
    assert prior.read_bytes() == fresh.read_bytes()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["export.out", "fresh.csv"]


def test_exports_refuse_an_existing_target_before_writing_anything(prior):
    """Refusal still happens up front, so the destination is never opened."""
    capture = capture_of(StepProfile([(100, 100.0, 0)]), samples=100)
    with pytest.raises(OutputExistsError):
        export_csv(capture, prior)
    with pytest.raises(OutputExistsError):
        export_samples_jsonl(capture, prior)
    assert prior.read_bytes() == b"previous good export\n"


# ---------------------------------------------------------------------------
# The raw export shape is frozen and unconditional
#
# A one-hour capture is 360 million samples and roughly 19 GB of CSV, which is
# a standing temptation to emit a summary instead "when the input is big". An
# export whose output shape depended on its input size would be derived data
# quietly standing in for raw data, and no consumer could tell which it got.

RAW_CSV_HEADER = (
    "timeline_index,time_s,current_ua,range,counter,"
    "d0,d1,d2,d3,d4,d5,d6,d7,valid,gap_before_missing\r\n"
)
RAW_CSV_HEADER_FILTERED = (
    "timeline_index,time_s,current_ua,current_filtered_ua,range,counter,"
    "d0,d1,d2,d3,d4,d5,d6,d7,valid,gap_before_missing\r\n"
)


def test_raw_csv_header_is_frozen(tmp_path):
    """Byte-for-byte. Consumers parse on these names, and the guarantee that a
    decimated file can never be read as a raw one rests on them."""
    capture = capture_of(StepProfile([(100, 100.0, 0)]), samples=100)
    plain, filtered = tmp_path / "p.csv", tmp_path / "f.csv"
    export_csv(capture, plain)
    export_csv(capture, filtered, include_filtered=True)
    assert plain.read_bytes().split(b"\r\n")[0] + b"\r\n" == RAW_CSV_HEADER.encode()
    assert filtered.read_bytes().split(b"\r\n")[0] + b"\r\n" == RAW_CSV_HEADER_FILTERED.encode()


def test_the_two_export_shapes_share_no_column_name():
    """A file has to be identifiable as raw or decimated from its first line."""
    raw = set(RAW_CSV_HEADER.strip().split(",")) | set(RAW_CSV_HEADER_FILTERED.strip().split(","))
    assert raw.isdisjoint(DECIMATED_CSV_HEADER)


def test_a_large_capture_still_exports_one_row_per_sample(tmp_path):
    """No input size turns `export --format csv` into a summary."""
    capture = capture_of(StepProfile([(100, 100.0, 0)], repeat=True), samples=8000)
    path = tmp_path / "big.csv"
    assert export_csv(capture, path) == capture.stored_count == 8000
    lines = path.read_bytes().split(b"\r\n")
    assert lines[0] + b"\r\n" == RAW_CSV_HEADER.encode()
    assert len([line for line in lines if line]) == 8001  # header + one per sample


# ---------------------------------------------------------------------------
# Decimated views
#
# The bucket record is frozen in docs/decimation.md. Two properties carry the
# whole design: every timeline position is accounted for exactly once, and the
# mean is defined by the charge rather than stored beside it.


def test_every_timeline_position_lands_in_exactly_one_bucket():
    """The counting invariant the record is built on."""
    capture = capture_of(StepProfile([(100, 100.0, 0)], repeat=True), samples=3000, gaps={1000: 7})
    buckets = list(iter_buckets(capture, bucket_samples=137))
    for bucket in buckets:
        assert (
            bucket.samples_in_bucket + bucket.missing_in_bucket + bucket.excluded_in_bucket
            == bucket.bucket_samples
        )
        assert sum(bucket.range_occupancy) == bucket.samples_in_bucket
    span = capture.end_index - capture.start_index
    assert sum(b.bucket_samples for b in buckets) == span
    assert sum(b.samples_in_bucket for b in buckets) == capture.stored_count
    assert sum(b.missing_in_bucket for b in buckets) == capture.missing_known


def test_a_bucket_over_a_gap_can_never_pass_for_a_full_one():
    """A bucket that reported only a mean would render lost time as measured
    time; the counts are what stop that."""
    capture = capture_of(StepProfile([(100, 100.0, 0)], repeat=True), samples=3000, gaps={1000: 40})
    buckets = {b.bucket_start_index: b for b in iter_buckets(capture, bucket_samples=250)}
    over_gap = buckets[1000]
    assert over_gap.missing_in_bucket == 40
    assert over_gap.samples_in_bucket == 210
    assert over_gap.complete is False
    assert buckets[750].complete is True and buckets[750].missing_in_bucket == 0


def test_bucket_boundaries_are_timeline_positions_not_stored_samples():
    """A gap must consume bucket space; if buckets counted stored samples the
    time axis would compress at every loss and t_start_s would be a fiction."""
    capture = capture_of(StepProfile([(100, 100.0, 0)], repeat=True), samples=3000, gaps={1000: 40})
    buckets = list(iter_buckets(capture, bucket_samples=250))
    assert [b.bucket_start_index for b in buckets] == list(range(0, 3000, 250))
    assert [b.t_start_s for b in buckets] == [
        pytest.approx(i / 100_000) for i in range(0, 3000, 250)
    ]


def test_the_bucket_mean_is_the_charge_over_the_samples_that_were_present():
    """`mean_ua == charge_uc / (n * 10 us)` is a definition, not a second
    statistic — which is what keeps re-decimation from compounding error."""
    capture = capture_of(
        StepProfile([(100, 6.0, 0), (100, 12000.0, 0)], repeat=True),
        samples=3000,
        gaps={1000: 40},
    )
    for bucket in iter_buckets(capture, bucket_samples=137):
        if bucket.samples_in_bucket:
            assert bucket.mean_ua == bucket.charge_uc / (bucket.samples_in_bucket * 1e-5)


def test_buckets_preserve_a_peak_a_mean_would_hide():
    """A per-bucket mean is a low-pass filter. A decimated plot that lost the
    burst is the thing an engineer would most regret trusting."""
    capture = capture_of(StepProfile([(30, 12000.0, 0), (970, 6.0, 0)], repeat=True), samples=3000)
    whole = compute_stats(capture)
    buckets = list(iter_buckets(capture, bucket_samples=1000))
    assert max(b.max_ua for b in buckets) == whole.max_ua
    assert min(b.min_ua for b in buckets) == whole.min_ua
    # and the mean of any bucket is a value the DUT never draws for a sample
    for bucket in buckets:
        assert bucket.min_ua < bucket.mean_ua < bucket.max_ua


def test_bucket_totals_reproduce_the_whole_window_statistics():
    capture = capture_of(StepProfile([(100, 6.0, 0), (100, 12000.0, 0)], repeat=True), samples=3000)
    whole = compute_stats(capture)
    buckets = list(iter_buckets(capture, bucket_samples=137))
    assert sum(b.charge_uc for b in buckets if b.charge_uc) == pytest.approx(
        whole.charge_uc, rel=1e-12
    )
    for index in range(5):
        assert sum(b.range_occupancy[index] for b in buckets) == whole.samples_per_range[index]
    # a switch is attributed to the bucket holding the later of the two
    # adjacent samples, including across a boundary, so the sum is the total
    assert sum(b.range_switches for b in buckets) == whole.range_switches


def test_merging_buckets_reproduces_a_wider_decimation():
    """Re-decimating a decimated series must not need the raw samples back."""
    capture = capture_of(
        StepProfile([(100, 6.0, 0), (100, 12000.0, 0)], repeat=True),
        samples=3000,
        gaps={1500: 40},
    )
    fine = list(iter_buckets(capture, bucket_samples=100))
    coarse = list(iter_buckets(capture, bucket_samples=300))
    assert len(fine) == 3 * len(coarse)
    for index, wide in enumerate(coarse):
        group = fine[index * 3 : index * 3 + 3]
        samples = sum(b.samples_in_bucket for b in group)
        charge = sum(b.charge_uc for b in group if b.charge_uc is not None)
        assert samples == wide.samples_in_bucket
        assert sum(b.missing_in_bucket for b in group) == wide.missing_in_bucket
        assert sum(b.range_switches for b in group) == wide.range_switches
        assert min(b.min_ua for b in group) == wide.min_ua
        assert max(b.max_ua for b in group) == wide.max_ua
        # Charge, and the mean re-derived from it, agree to float summation
        # order — the residual does not grow with further passes.
        assert charge == pytest.approx(wide.charge_uc, rel=1e-12)
        assert charge / (samples * 1e-5) == pytest.approx(wide.mean_ua, rel=1e-12)


def test_a_bucket_with_no_usable_sample_reports_null_rather_than_zero():
    """Zero would claim the DUT drew nothing across a span where nothing was
    measured at all."""
    capture = capture_of(StepProfile([(100, 100.0, 0)], repeat=True), samples=600)
    capture.gaps.append(GapEvent(600, 400, "host_overflow"))
    buckets = {b.bucket_start_index: b for b in iter_buckets(capture, bucket_samples=200)}
    empty = buckets[800]
    assert empty.samples_in_bucket == 0 and empty.missing_in_bucket == 200
    assert empty.mean_ua is None and empty.charge_uc is None
    assert empty.min_ua is None and empty.max_ua is None
    assert empty.complete is False


def test_a_gap_of_undetermined_size_still_costs_a_bucket_its_completeness():
    """A desync gap occupies no timeline width, so it consumes no bucket
    positions and shows up in none of the width-based counts. Data was still
    lost there, and a bucket that reported `complete` over one would be
    claiming a span it cannot account for."""
    base = capture_of(StepProfile([(100, 100.0, 0)], repeat=True), samples=400)
    capture = Capture(base.meta, base.words, [GapEvent(200, None, "stream_desync")])
    buckets = {b.bucket_start_index: b for b in iter_buckets(capture, bucket_samples=100)}
    over_desync = buckets[200]
    assert over_desync.unknown_gaps_in_bucket == 1
    assert over_desync.missing_in_bucket == 0  # nothing measurable was skipped
    assert over_desync.samples_in_bucket == 100
    assert over_desync.complete is False
    assert buckets[300].complete is True


def test_a_delivered_but_unusable_sample_is_not_counted_as_missing():
    """An invalid range field and a sample that never arrived are different
    failures with different remedies; folding either into the present count
    would dilute a mean it carries no charge towards."""
    base = capture_of(StepProfile([(100, 100.0, 0)], repeat=True), samples=400)
    words = array("I", base.words)
    for index in (10, 11, 12):
        words[index] = pack_sample(1000, 6, 0, 0)  # range 6 does not exist
    capture = Capture(base.meta, words, [])
    bucket = next(iter(iter_buckets(capture, bucket_samples=100)))
    assert bucket.excluded_in_bucket == 3
    assert bucket.missing_in_bucket == 0
    assert bucket.samples_in_bucket == 97
    assert bucket.complete is False
    assert bucket.mean_ua == bucket.charge_uc / (97 * 1e-5)


def test_a_pinned_peak_is_marked_so_it_is_not_read_as_a_measurement():
    """A sample on the ADC's full-scale code is a ceiling. Without this counter
    a decimated file renders a clipped burst as a clean flat top."""
    capture = capture_of(ConstantProfile(2_500_000.0), samples=500)
    buckets = list(iter_buckets(capture, bucket_samples=100))
    assert all(b.saturated_in_bucket == b.samples_in_bucket for b in buckets)
    assert compute_stats(capture).saturated_samples == sum(b.saturated_in_bucket for b in buckets)
    ordinary = capture_of(StepProfile([(100, 100.0, 0)], repeat=True), samples=500)
    assert all(b.saturated_in_bucket == 0 for b in iter_buckets(ordinary, bucket_samples=100))


def test_a_range_change_across_a_gap_is_not_an_observed_switch():
    """Two samples separated by missing data were not adjacent."""
    capture = capture_of(
        StepProfile([(500, 6.0, 0), (500, 12000.0, 0)], repeat=True),
        samples=1000,
        gaps={500: 4},
    )
    buckets = list(iter_buckets(capture, bucket_samples=1000))
    assert buckets[0].range_switches == compute_stats(capture).range_switches == 0


@pytest.mark.parametrize(
    ("bucket_ms", "expected"), [(1.0, 100), (0.01, 1), (10.0, 1000), (0.5, 50)]
)
def test_a_bucket_width_in_milliseconds_resolves_at_the_recorded_rate(bucket_ms, expected):
    assert bucket_samples_for_ms(bucket_ms) == expected


@pytest.mark.parametrize("bucket_ms", [0.0, -1.0, 0.001, float("nan")])
def test_a_bucket_narrower_than_one_sample_is_refused(bucket_ms):
    """Rounding it up to one sample would silently ignore what was asked for."""
    with pytest.raises(UsageError):
        bucket_samples_for_ms(bucket_ms)


def test_decimated_csv_and_jsonl_carry_the_same_record(tmp_path):
    capture = capture_of(
        StepProfile([(100, 6.0, 0), (100, 12000.0, 0)], repeat=True),
        samples=3000,
        gaps={1000: 40},
    )
    csv_path, jsonl_path = tmp_path / "d.csv", tmp_path / "d.jsonl"
    assert export_decimated_csv(capture, csv_path, bucket_samples=250) == 12
    assert export_decimated_jsonl(capture, jsonl_path, bucket_samples=250) == 12
    with open(csv_path) as fh:
        rows = list(csv.DictReader(fh))
    records = [json.loads(line)["bucket"] for line in jsonl_path.read_text().splitlines()]
    assert list(rows[0]) == list(DECIMATED_CSV_HEADER)
    for row, record in zip(rows, records, strict=True):
        assert int(row["bucket_start_index"]) == record["bucket_start_index"]
        assert int(row["samples_in_bucket"]) == record["samples_in_bucket"]
        assert int(row["missing_in_bucket"]) == record["missing_in_bucket"]
        assert (row["complete"] == "1") is record["complete"]
        # full round-trip precision, so the mean/charge identity is checkable
        # from the CSV itself rather than only from the JSONL
        assert float(row["charge_uc"]) == record["charge_uc"]
        assert float(row["mean_ua"]) == record["mean_ua"]


def test_a_decimated_jsonl_line_cannot_be_read_as_a_raw_one(tmp_path):
    capture = capture_of(StepProfile([(100, 100.0, 0)], repeat=True), samples=600)
    raw, decimated = tmp_path / "r.jsonl", tmp_path / "d.jsonl"
    export_samples_jsonl(capture, raw)
    export_decimated_jsonl(capture, decimated, bucket_samples=100)
    raw_keys = {key for line in raw.read_text().splitlines() for key in json.loads(line)}
    dec_keys = {key for line in decimated.read_text().splitlines() for key in json.loads(line)}
    assert dec_keys == {"bucket"}
    assert raw_keys.isdisjoint(dec_keys)


def test_a_failed_decimated_export_leaves_the_previous_one_intact(tmp_path, prior):
    capture = _fails_after(capture_of(StepProfile([(2000, 100.0, 0)]), samples=2000), 500)
    with pytest.raises(_DiskFull):
        export_decimated_csv(capture, prior, bucket_samples=100, overwrite=True)
    assert prior.read_bytes() == b"previous good export\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["export.out"]


def test_a_short_decimated_export_is_refused_before_it_replaces_anything(tmp_path, prior):
    """The bucket count follows from the timeline span alone, so a mismatch
    means the file's time axis would be silently short."""
    capture = _truncates_after(capture_of(StepProfile([(2000, 100.0, 0)]), samples=2000), 500)
    with pytest.raises(CaptureFileError):
        export_decimated_jsonl(capture, prior, bucket_samples=100, overwrite=True)
    assert prior.read_bytes() == b"previous good export\n"


# ---------------------------------------------------------------------------
# Size estimates
#
# One hour is roughly 19 GB of CSV. "How big will this be" deserves an answer
# before the write starts rather than after the disk fills.


@pytest.mark.parametrize("export_format", ["csv", "jsonl"])
def test_the_size_estimate_is_measured_on_this_captures_own_records(tmp_path, export_format):
    """Row width depends on the values, so a compiled-in average would be a
    guess presented as a fact."""
    capture = capture_of(StepProfile([(100, 6.0, 0), (100, 12000.0, 0)], repeat=True), samples=6000)
    estimate = estimate_export_size(capture, export_format=export_format)
    path = tmp_path / f"out.{export_format}"
    if export_format == "csv":
        assert export_csv(capture, path) == estimate["records"]
    else:
        assert export_samples_jsonl(capture, path) == estimate["records"]
    actual = path.stat().st_size
    assert estimate["estimated_bytes"] == pytest.approx(actual, rel=0.05)
    assert estimate["bytes_per_record"] > 0
    assert "measured" in estimate["basis"]


def test_the_size_estimate_covers_the_decimated_view_too(tmp_path):
    capture = capture_of(StepProfile([(100, 100.0, 0)], repeat=True), samples=6000)
    estimate = estimate_export_size(capture, export_format="csv", bucket_samples=100)
    assert estimate["decimated"] is True
    path = tmp_path / "d.csv"
    assert export_decimated_csv(capture, path, bucket_samples=100) == estimate["records"] == 60
    assert estimate["estimated_bytes"] == pytest.approx(path.stat().st_size, rel=0.05)


def test_the_estimator_reports_nothing_it_cannot_measure():
    """A VCD carries one record per logic-line change, which is a property of
    the DUT. Null beats a number derived from an assumption about someone
    else's firmware."""
    capture = capture_of(StepProfile([(100, 100.0, 0)]), samples=100)
    estimate = estimate_export_size(capture, export_format="vcd")
    assert estimate["records"] is None
    assert estimate["estimated_bytes"] is None
    assert "not estimated" in estimate["basis"]


def test_estimating_an_empty_capture_promises_only_a_header():
    capture = Capture(capture_of(StepProfile([(1, 1.0, 0)]), samples=1).meta, array("I"), [])
    estimate = estimate_export_size(capture, export_format="csv")
    assert estimate["records"] == 0
    assert estimate["bytes_per_record"] is None
    assert estimate["estimated_bytes"] == len(RAW_CSV_HEADER.encode())
