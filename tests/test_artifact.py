"""Canonical artifact: roundtrip, integrity, overwrite policy, malformed input."""

import json
import os
import zipfile

import pytest

from ppk2lab.capture import artifact as artifact_module
from ppk2lab.capture.artifact import (
    W_PARTIAL_INTEGRITY,
    ArtifactReader,
    ArtifactWriter,
    read_capture,
    read_window,
    write_capture,
)
from ppk2lab.capture.model import Capture
from ppk2lab.diagnostics import W_MANIFEST_IMPLAUSIBLE
from ppk2lab.errors import (
    CaptureFileError,
    CaptureTooLargeError,
    OutputExistsError,
    UsageError,
)
from ppk2lab.testing.profiles import StepProfile

from .conftest import capture_of


def _rewrite_manifest(path, mutate):
    """Rebuild an artifact with a mutated manifest, leaving chunks intact."""
    with zipfile.ZipFile(path) as zf:
        items = {name: zf.read(name) for name in zf.namelist()}
    manifest = json.loads(items["manifest.json"])
    mutate(manifest)
    items["manifest.json"] = json.dumps(manifest).encode()
    with zipfile.ZipFile(path, "w") as zf:
        for name, blob in items.items():
            zf.writestr(zipfile.ZipInfo(name), blob)
    return manifest


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
    _rewrite_manifest(path, lambda m: m.__setitem__("format_version", 99))
    with pytest.raises(CaptureFileError) as excinfo:
        read_capture(path)
    assert "newer" in str(excinfo.value)


@pytest.mark.parametrize("value", [None, "1", 1.0, True])
def test_format_version_must_be_a_real_integer(tmp_path, capture, value):
    """An absent or non-integer version was accepted silently, and `True` is
    an int in Python — it would have sailed through as version 1."""

    def mutate(manifest):
        if value is None:
            del manifest["format_version"]
        else:
            manifest["format_version"] = value

    path = tmp_path / "cap.ppk2a"
    capture.save(str(path))
    _rewrite_manifest(path, mutate)
    with pytest.raises(CaptureFileError) as excinfo:
        read_capture(path)
    assert "format_version" in str(excinfo.value)


# ---------------------------------------------------------------------------
# The timing block
#
# The 6-bit counter cannot describe a loss of 64 or more samples, so wall-clock
# elapsed time is the only independent witness. It is recorded while the stream
# runs and can never be recomputed afterwards: the loss it describes is exactly
# the loss the stored samples do not contain.


def test_timeline_block_survives_a_read_write_round_trip(tmp_path, capture):
    """Reading an artifact and writing it back deleted 7 of the timeline
    block's 11 keys, erasing the deficit the first time anyone re-wrote a
    capture."""
    original = tmp_path / "cap.ppk2a"
    capture.save(str(original))
    with zipfile.ZipFile(original) as zf:
        before = json.loads(zf.read("manifest.json"))["timeline"]

    reloaded = read_capture(original)
    copy = tmp_path / "copy.ppk2a"
    write_capture(reloaded, copy)
    with zipfile.ZipFile(copy) as zf:
        after = json.loads(zf.read("manifest.json"))["timeline"]

    assert set(after) == set(before)
    assert after == before


def test_in_memory_capture_save_writes_the_timing_block(tmp_path):
    """`capture.save()` never wrote the timing block at all, so the whole
    in-memory path lost the witness the streaming path recorded."""
    from ppk2lab.device import PPK2
    from ppk2lab.transport.mock import MockTransport, SimulatedPPK2

    device = PPK2.open(transport=MockTransport(SimulatedPPK2()), simulate=True)
    try:
        result = device.capture(duration_s=0.05)
    finally:
        device.close()
    path = tmp_path / "mem.ppk2a"
    result.capture.save(str(path))
    with zipfile.ZipFile(path) as zf:
        timeline = json.loads(zf.read("manifest.json"))["timeline"]
    assert timeline["rate_check"] == result.timeline["rate_check"]
    assert timeline["timeline_advance"] == result.timeline["timeline_advance"]
    assert timeline["unaccounted_samples_estimate"] is not None


def test_gaps_truncated_is_written_and_read_back(tmp_path, capture):
    """The field was written by the writer and read by nothing, so the count
    of gaps nobody enumerated existed only inside the file."""
    stored = Capture(
        capture.meta,
        capture.words,
        capture.gaps,
        complete=False,
        gaps_truncated=42,
    )
    path = tmp_path / "trunc.ppk2a"
    write_capture(stored, path)
    with zipfile.ZipFile(path) as zf:
        manifest = json.loads(zf.read("manifest.json"))
    assert manifest["gaps_truncated"] == 42
    assert manifest["timeline"]["degraded"] is True
    reloaded = read_capture(path)
    assert reloaded.gaps_truncated == 42
    assert reloaded.timeline_degraded


# ---------------------------------------------------------------------------
# Malformed manifests
#
# Structure is an error: an unreadable gap table makes every timeline position
# downstream meaningless. Content that is merely impossible is a warning
# reported verbatim — clamping it would replace a manifest known to be wrong
# with a number that only looks right.


@pytest.mark.parametrize(
    "gaps",
    [
        "not a list",
        [42],
        [{"missing": 4}],  # no index
        [{"index": -1, "missing": 4}],
        [{"index": True, "missing": 4}],
        [{"index": 4}],  # absent missing is not the same claim as zero
        [{"index": 4, "missing": -1}],
        [{"index": 4, "missing": "many"}],
    ],
)
def test_structurally_invalid_gap_tables_are_refused(tmp_path, capture, gaps):
    path = tmp_path / "cap.ppk2a"
    capture.save(str(path))
    _rewrite_manifest(path, lambda m: m.__setitem__("gaps", gaps))
    with pytest.raises(CaptureFileError):
        read_capture(path)


def test_a_gap_past_the_end_of_the_timeline_warns_rather_than_failing(tmp_path, capture):
    """The writer legitimately records a gap sitting exactly on the end bound,
    so only an index beyond it is impossible — and an impossible index is not
    a reason to refuse the samples that are perfectly readable."""
    path = tmp_path / "cap.ppk2a"
    capture.save(str(path))
    _rewrite_manifest(
        path,
        lambda m: m["gaps"].append(
            {"index": 10**9, "missing": 1, "reason": "counter_skip", "ambiguous": True}
        ),
    )
    loaded = read_capture(path)
    assert any(w["code"] == W_MANIFEST_IMPLAUSIBLE for w in loaded.warnings)
    assert any(g.index == 10**9 for g in loaded.gaps)  # reported as stored


def test_a_missing_total_the_clock_cannot_support_warns_and_is_not_clamped(tmp_path, capture):
    """`missing: 10**18` produced duration_s: 10000000000000.2 with ok: true.
    Laundering it into a plausible number would be worse than reporting it:
    the manifest is wrong and the reader has to know that."""
    path = tmp_path / "cap.ppk2a"
    capture.save(str(path))
    _rewrite_manifest(
        path,
        lambda m: (
            m["timeline"].__setitem__("wall_elapsed_s", 0.012),
            m["gaps"].__setitem__(
                0, {"index": 600, "missing": 10**18, "reason": "counter_skip", "ambiguous": True}
            ),
        ),
    )
    loaded = read_capture(path)
    assert any(w["code"] == W_MANIFEST_IMPLAUSIBLE for w in loaded.warnings)
    assert loaded.missing_known == 10**18  # verbatim, not clamped


@pytest.mark.parametrize("field", ["stored_count", "encoding"])
def test_unusable_samples_block_is_refused(tmp_path, capture, field):
    path = tmp_path / "cap.ppk2a"
    capture.save(str(path))
    _rewrite_manifest(path, lambda m: m["samples"].pop(field))
    with pytest.raises(CaptureFileError):
        read_capture(path)


# ---------------------------------------------------------------------------
# Paths that are not files, and files too large to hold


def test_a_directory_is_an_ordinary_error_not_a_crash(tmp_path):
    """A directory reached zipfile as an ordinary path and exited 9."""
    target = tmp_path / "out.ppk2a"
    target.mkdir()
    with pytest.raises(CaptureFileError) as excinfo:
        read_capture(target)
    assert excinfo.value.exit_code == 2


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs on this platform")
def test_a_fifo_is_refused_instead_of_blocking_forever(tmp_path):
    """Opening a FIFO with no writer blocks in the kernel; a capture command
    that never returns is worse than one that fails."""
    fifo = tmp_path / "pipe.ppk2a"
    os.mkfifo(fifo)
    with pytest.raises(CaptureFileError) as excinfo:
        read_capture(fifo)
    assert "regular file" in str(excinfo.value)


def test_a_capture_too_large_to_load_is_not_reported_as_corrupt(tmp_path, capture):
    """CAPTURE_FILE_INVALID told an agent an 8-hour soak artifact needed
    repair. It needs a window, not a repair."""
    path = tmp_path / "cap.ppk2a"
    capture.save(str(path))
    with pytest.raises(CaptureTooLargeError) as excinfo:
        read_capture(path, max_samples=10)
    assert excinfo.value.code == "CAPTURE_TOO_LARGE"
    assert excinfo.value.exit_code == 2
    # every remedy the message names has to exist
    assert "--max-samples" in excinfo.value.remediation
    assert "inspect" in excinfo.value.remediation
    read_capture(path, max_samples=None)  # the opt-out works


def test_capture_load_carries_the_same_ceiling(tmp_path, capture):
    path = tmp_path / "cap.ppk2a"
    capture.save(str(path))
    with pytest.raises(CaptureTooLargeError):
        Capture.load(str(path), max_samples=10)
    assert Capture.load(str(path), max_samples=None).stored_count == capture.stored_count
    assert Capture.load(str(path)).stored_count == capture.stored_count


# ---------------------------------------------------------------------------
# Finalize: a complete capture is never deleted to tidy up


def test_a_failed_rename_preserves_the_finished_capture(tmp_path, capture, monkeypatch):
    """After the container is closed the temp file IS a complete, readable
    capture. Aborting here would destroy bench time that cannot be
    re-acquired, so the error names the file instead."""
    path = tmp_path / "cap.ppk2a"

    def refuse(_src, _dst):
        raise OSError(18, "Invalid cross-device link")

    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(CaptureFileError) as excinfo:
        write_capture(capture, path)

    leftovers = list(tmp_path.iterdir())
    assert len(leftovers) == 1, "the finished capture must not be deleted"
    temp = leftovers[0]
    assert str(temp) in excinfo.value.remediation
    monkeypatch.undo()
    # And it really is a capture: readable, with its samples intact.
    recovered = read_capture(temp)
    assert recovered.sha256() == capture.sha256()


def test_a_failure_before_the_manifest_leaves_nothing_behind(tmp_path, capture, monkeypatch):
    """The other half of the same rule: with no manifest the temp file is not
    a capture, and keeping it helps nobody."""
    path = tmp_path / "cap.ppk2a"
    writer = ArtifactWriter(path, capture.meta)
    monkeypatch.setattr(
        type(writer._zip), "writestr", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
    )
    with pytest.raises(OSError):
        writer.finalize()
    assert list(tmp_path.iterdir()) == []


def test_an_output_path_that_is_a_directory_is_refused_before_capturing(tmp_path, capture):
    """`mkdir out.ppk2a; capture --overwrite` streamed a whole capture and
    then failed on the rename. The mistake is visible before the first sample
    arrives, which is the only point at which refusing costs nothing."""
    target = tmp_path / "out.ppk2a"
    target.mkdir()
    with pytest.raises(UsageError) as excinfo:
        ArtifactWriter(target, capture.meta, overwrite=True)
    assert excinfo.value.exit_code == 2
    assert list(target.iterdir()) == []


# ---------------------------------------------------------------------------
# Windowed reads
#
# `read_capture` materializes every sample plus a digest copy, so it is refused
# past roughly 250 s of recording — and an eight-hour soak is a release gate.
# A windowed read touches only the chunks its window falls in, so peak memory
# follows the window instead of the file. What it must never do is answer a
# question it did not verify, or a different question than the one asked.


def _windowable(tmp_path, monkeypatch, *, samples=3000, chunk=500, gaps=None):
    """An artifact with several chunks, so windowing has something to skip."""
    monkeypatch.setattr(artifact_module, "CHUNK_SAMPLES", chunk)
    capture = capture_of(StepProfile([(100, 100.0, 0x03)], repeat=True), samples=samples, gaps=gaps)
    path = tmp_path / "many.ppk2a"
    capture.save(str(path))
    return path


def _chunk_reads(monkeypatch):
    """Record which chunk members a reader actually pulls out of the zip."""
    names: list[str] = []
    original = zipfile.ZipFile.read

    def spy(self, name, *args, **kwargs):
        label = name if isinstance(name, str) else name.filename
        if label.startswith("chunks/"):
            names.append(label)
        return original(self, name, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "read", spy)
    return names


def test_a_window_holds_exactly_what_the_same_window_of_a_full_read_holds(tmp_path, monkeypatch):
    """The point of the window is the memory ceiling, not a different answer."""
    path = _windowable(tmp_path, monkeypatch)
    full = read_capture(path)
    window = read_window(path, start_index=700, end_index=2200)
    assert window.start_index == 700
    assert window.stored_count == 1500
    assert list(window.words) == list(full.words[700:2200])
    assert window.currents_ua() == full.currents_ua()[700:2200]


def test_a_window_reads_only_the_chunks_it_falls_in(tmp_path, monkeypatch):
    """This is the whole feature: an hour-long capture must not be paid for to
    look at half a second of it."""
    path = _windowable(tmp_path, monkeypatch, samples=3000, chunk=500)
    reads = _chunk_reads(monkeypatch)
    read_window(path, start_index=1200, end_index=1400)
    assert reads == ["chunks/000002.u32"]
    reads.clear()
    read_capture(path)
    assert len(reads) == 6  # 3000 samples / 500 per chunk


def test_a_window_does_not_claim_an_integrity_check_it_did_not_run(tmp_path, monkeypatch):
    """Only the chunks touched were CRC-checked. Emitting the manifest's
    whole-file SHA-256 next to that would assert a check nobody performed."""
    path = _windowable(tmp_path, monkeypatch)
    window = read_window(path, start_index=1000, end_index=1200)
    assert window.artifact_sha256 is None
    codes = [w["code"] for w in window.warnings if isinstance(w, dict)]
    assert W_PARTIAL_INTEGRITY in codes


def test_a_full_read_carries_the_digest_it_verified_without_hashing_twice(tmp_path, monkeypatch):
    """The digest is folded in chunk by chunk while the words are read.
    Hashing the assembled capture afterwards builds a second full copy of the
    samples — a doubled peak on the largest thing the process holds, paid
    milliseconds before the caller asks for the digest anyway."""
    path = _windowable(tmp_path, monkeypatch)
    with zipfile.ZipFile(path) as zf:
        recorded = json.loads(zf.read("manifest.json"))["samples"]["sha256"]

    def refuse(self):
        raise AssertionError("the sample bytes were copied to be hashed again")

    monkeypatch.setattr(Capture, "raw_bytes", refuse)
    loaded = read_capture(path)
    assert loaded.artifact_sha256 == recorded
    assert loaded.sha256() == recorded  # seeded, so it costs nothing to ask


def test_a_window_starting_inside_a_gap_is_refused_rather_than_moved(tmp_path, monkeypatch):
    """There is no sample at that position to start from, and starting at the
    next one would silently answer a different question."""
    path = _windowable(tmp_path, monkeypatch, gaps={1000: 40})
    with pytest.raises(UsageError) as excinfo:
        read_window(path, start_index=1010, end_index=1500)
    assert excinfo.value.exit_code == 2
    # both edges of the gap are named, so the caller can pick one deliberately
    assert "1,000" in str(excinfo.value) and "1,040" in excinfo.value.remediation
    # and the position where the samples resume is a perfectly good start
    assert read_window(path, start_index=1040, end_index=1500).stored_count == 460


def test_a_window_ending_inside_a_gap_keeps_the_samples_before_it(tmp_path, monkeypatch):
    """Only the start bound has no answer inside a gap; an end bound has one."""
    path = _windowable(tmp_path, monkeypatch, gaps={1000: 40})
    window = read_window(path, start_index=900, end_index=1020)
    assert window.stored_count == 100  # 900..999; nothing exists at 1000..1019
    assert [g.to_json()["missing"] for g in window.gaps] == [40]


def test_a_window_carries_the_gaps_the_capture_never_enumerated(tmp_path, monkeypatch):
    """A truncated gap table means timeline positions cannot all be resolved.
    A window read out of such a capture inherits that, so the count travels."""
    monkeypatch.setattr(artifact_module, "CHUNK_SAMPLES", 500)
    base = capture_of(StepProfile([(100, 100.0, 0)], repeat=True), samples=2000)
    stored = Capture(base.meta, base.words, [], complete=False, gaps_truncated=17)
    path = tmp_path / "trunc.ppk2a"
    write_capture(stored, path)
    window = read_window(path, start_index=500, end_index=1000)
    assert window.gaps_truncated == 17
    assert window.timeline_degraded


def test_a_window_keeps_the_capture_wide_wall_clock_witness(tmp_path, monkeypatch):
    """The deficit the wall clock witnessed belongs to the whole capture and
    cannot be recomputed from a slice — the loss it describes is exactly what
    the stored samples do not contain."""
    path = _windowable(tmp_path, monkeypatch)
    full = read_capture(path)
    window = read_window(path, start_index=500, end_index=1500)
    assert window.timing == full.timing
    assert window.timing  # the fixture really does carry a timing block


def test_a_window_keeps_the_capture_time_origin(tmp_path, monkeypatch):
    """`time_s` in a windowed export has to mean what it means in a full one.
    An origin reset to the window start makes the same sample read 0.0 s in
    one file and 10.0 s in the other."""
    path = _windowable(tmp_path, monkeypatch)
    full = read_capture(path)
    window = read_window(path, start_index=1000, end_index=1200)
    assert window.time_origin_index == full.time_origin_index == 0
    assert window.index_to_time(1000) == full.index_to_time(1000) == pytest.approx(0.01)


def test_window_bounds_may_be_given_in_seconds(tmp_path, monkeypatch):
    """Resolved against the artifact's own recorded rate, not a compiled-in
    one: the stored timeline block is the authority on what it ran at."""
    path = _windowable(tmp_path, monkeypatch)
    by_seconds = read_window(path, start_s=0.005, end_s=0.015)
    by_index = read_window(path, start_index=500, end_index=1500)
    assert list(by_seconds.words) == list(by_index.words)
    with pytest.raises(UsageError):
        read_window(path, start_index=0, start_s=0.0, end_index=100)
    with pytest.raises(UsageError):
        read_window(path, start_s=float("nan"), end_s=1.0)


def test_a_window_past_the_end_is_unpopulated_rather_than_complete(tmp_path, monkeypatch):
    """A1's coverage rule has to survive the windowed path: measuring the
    returned capture over the window that was asked for still reports the
    positions the capture never reached."""
    from ppk2lab.capture.stats import compute_stats

    path = _windowable(tmp_path, monkeypatch)
    window = read_window(path, start_index=2800, end_index=5000)
    stats = compute_stats(window, start_index=2800, end_index=5000)
    assert stats.stored_samples == 200
    assert stats.unpopulated_samples == 2000
    assert stats.complete is False
    assert stats.charge_is_lower_bound is True


def test_a_window_too_large_to_hold_is_refused_by_the_same_ceiling(tmp_path, monkeypatch):
    path = _windowable(tmp_path, monkeypatch)
    with pytest.raises(CaptureTooLargeError):
        read_window(path, start_index=0, end_index=3000, max_samples=10)
    assert read_window(path, start_index=0, end_index=3000, max_samples=None).stored_count == 3000


def test_an_inverted_or_empty_window_is_a_usage_error(tmp_path, monkeypatch):
    path = _windowable(tmp_path, monkeypatch)
    with pytest.raises(UsageError):
        read_window(path, start_index=1500, end_index=1500)
    with pytest.raises(UsageError):
        read_window(path, start_index=1500, end_index=900)


def test_reader_streaming_iteration(tmp_path, capture):
    path = tmp_path / "cap.ppk2a"
    capture.save(str(path))
    with ArtifactReader(path) as reader:
        total = sum(len(words) for _, words in reader.iter_raw_words())
        assert total == capture.stored_count
        assert reader.verify_sha256()
