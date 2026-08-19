"""Canonical artifact: roundtrip, integrity, overwrite policy, malformed input."""

import json
import zipfile

import pytest

from ppk2lab.capture.artifact import ArtifactReader, read_capture
from ppk2lab.errors import CaptureFileError, OutputExistsError
from ppk2lab.testing.profiles import StepProfile

from .conftest import capture_of

try:
    import jsonschema
except ImportError:  # pragma: no cover
    jsonschema = None


@pytest.fixture
def capture():
    return capture_of(StepProfile([(500, 250.0, 0x55)], repeat=True), samples=1200, gaps={600: 12})


def test_roundtrip_preserves_everything(tmp_path, capture):
    path = tmp_path / "cap.ppk2a"
    capture.save(str(path))
    loaded = read_capture(path)
    assert loaded.sha256() == capture.sha256()
    assert loaded.stored_count == capture.stored_count
    assert [g.to_json() for g in loaded.gaps] == [g.to_json() for g in capture.gaps]
    assert loaded.complete == capture.complete
    assert loaded.meta.capture_id == capture.meta.capture_id
    assert loaded.meta.metadata_text == capture.meta.metadata_text
    assert loaded.currents_ua()[:10] == pytest.approx(capture.currents_ua()[:10])


def test_never_overwrites_without_flag(tmp_path, capture):
    path = tmp_path / "cap.ppk2a"
    capture.save(str(path))
    with pytest.raises(OutputExistsError):
        capture.save(str(path))
    capture.save(str(path), overwrite=True)  # explicit overwrite allowed


def test_no_temp_files_left_behind(tmp_path, capture):
    path = tmp_path / "cap.ppk2a"
    capture.save(str(path))
    leftovers = [p for p in tmp_path.iterdir() if p.name != "cap.ppk2a"]
    assert leftovers == []


def test_manifest_contents(tmp_path, capture):
    path = tmp_path / "cap.ppk2a"
    capture.save(str(path))
    with zipfile.ZipFile(path) as zf:
        manifest = json.loads(zf.read("manifest.json"))
        names = zf.namelist()
    assert "metadata.txt" in names
    assert manifest["format"] == "ppk2lab-capture"
    assert manifest["format_version"] == 1
    assert manifest["samples"]["stored_count"] == capture.stored_count
    assert manifest["complete"] is False  # this capture has a gap
    assert manifest["gaps"][0]["missing"] == 12
    assert manifest["device"]["simulated"] is True
    assert manifest["configuration"]["sample_rate_hz"] == 100000
    if jsonschema is not None:
        from ppk2lab.schemas import get_schema

        jsonschema.validate(manifest, get_schema("capture-manifest"))


def test_corrupted_chunk_detected(tmp_path, capture):
    path = tmp_path / "cap.ppk2a"
    capture.save(str(path))
    # rebuild the zip with a flipped byte inside the first chunk
    with zipfile.ZipFile(path) as zf:
        items = {name: zf.read(name) for name in zf.namelist()}
    chunk_name = next(n for n in items if n.startswith("chunks/"))
    data = bytearray(items[chunk_name])
    data[10] ^= 0xFF
    items[chunk_name] = bytes(data)
    with zipfile.ZipFile(path, "w") as zf:
        for name, blob in items.items():
            zf.writestr(zipfile.ZipInfo(name), blob)
    with pytest.raises(CaptureFileError):
        read_capture(path)


def test_not_a_zip_rejected(tmp_path):
    path = tmp_path / "junk.ppk2a"
    path.write_bytes(b"this is not a capture")
    with pytest.raises(CaptureFileError):
        read_capture(path)


def test_missing_file_rejected(tmp_path):
    with pytest.raises(CaptureFileError):
        read_capture(tmp_path / "absent.ppk2a")


def test_newer_format_version_refused(tmp_path, capture):
    path = tmp_path / "cap.ppk2a"
    capture.save(str(path))
    with zipfile.ZipFile(path) as zf:
        items = {name: zf.read(name) for name in zf.namelist()}
    manifest = json.loads(items["manifest.json"])
    manifest["format_version"] = 99
    items["manifest.json"] = json.dumps(manifest).encode()
    with zipfile.ZipFile(path, "w") as zf:
        for name, blob in items.items():
            zf.writestr(zipfile.ZipInfo(name), blob)
    with pytest.raises(CaptureFileError) as excinfo:
        read_capture(path)
    assert "newer" in str(excinfo.value)


def test_reader_streaming_iteration(tmp_path, capture):
    path = tmp_path / "cap.ppk2a"
    capture.save(str(path))
    with ArtifactReader(path) as reader:
        total = sum(len(words) for _, words in reader.iter_raw_words())
        assert total == capture.stored_count
        assert reader.verify_sha256()
