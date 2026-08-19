"""Window/annotation measurement and CSV/JSONL/VCD exports."""

import csv
import json

import pytest

from ppk2lab.analysis.measure import (
    load_annotations_jsonl,
    measure_annotations,
    measure_window,
    save_annotations_jsonl,
)
from ppk2lab.decoders.base import Annotation
from ppk2lab.errors import CaptureFileError, OutputExistsError
from ppk2lab.exports import export_csv, export_samples_jsonl
from ppk2lab.testing.profiles import StepProfile

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
