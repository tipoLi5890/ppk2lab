"""Canonical artifact: roundtrip, integrity, overwrite policy, malformed input."""

import json
import os
import stat
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
    # Every field the streaming path recorded, not two of them: the bug was
    # that `save()` wrote no timing at all. Containment rather than equality,
    # because the manifest block is the timing report plus the timeline's own
    # sample_rate_hz/sample_period_ns/start_index/degraded.
    #
    # `unaccounted_samples_estimate` is deliberately not asserted non-null. It
    # is None whenever both anchors were stamped at the same instant, and this
    # capture spans about 13 ms — under the ~15.6 ms granularity
    # `time.monotonic` has on Windows before Python 3.13, where the honest
    # answer really is that no interval was measured.
    assert result.timeline
    assert timeline.items() >= result.timeline.items()
    assert timeline["timeline_advance"] == 5000


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


# -- the writer thread ---------------------------------------------------
#
# Compressing a chunk inline used to stall the thread feeding samples in for
# as long as the deflate took (~290 ms for 4 MB), which overflowed the
# reader's queue and made the artifact writer the source of the very
# host_overflow gaps the capture reported. These pin the arrangement that
# fixed it.


def _one_block(samples, start_index=0):
    from array import array

    from ppk2lab.protocol.samples import SampleBlock

    return SampleBlock(start_index=start_index, words=array("I", [0] * samples))


def test_compression_does_not_run_on_the_thread_feeding_samples(tmp_path, monkeypatch):
    """A slow write must not block the producer: that is the whole fix."""
    import threading
    import time

    from ppk2lab.capture.model import CaptureMeta

    monkeypatch.setattr(artifact_module, "CHUNK_SAMPLES", 1000)
    writer = ArtifactWriter(tmp_path / "cap.ppk2a", CaptureMeta())
    producer_thread = threading.get_ident()
    seen: list[int] = []
    real_writestr = writer._zip.writestr

    def slow_writestr(name, data):
        # Only chunk writes are the hot path; the manifest is written on the
        # caller's thread after the writer has been joined, and should be.
        if name.startswith("chunks/"):
            seen.append(threading.get_ident())
            time.sleep(0.3)
        return real_writestr(name, data)

    monkeypatch.setattr(writer._zip, "writestr", slow_writestr)

    started = time.monotonic()
    writer.add_block(_one_block(1000))
    elapsed = time.monotonic() - started

    assert elapsed < 0.15, f"add_block blocked for {elapsed:.3f}s on the compressor"
    writer.finalize(complete=True)
    assert seen, "the chunk was never written"
    assert producer_thread not in seen, "compression ran on the producer's thread"


def test_a_failing_writer_thread_surfaces_and_never_passes_silently(tmp_path, monkeypatch):
    """A chunk that could not be written must not leave a lying manifest."""
    from ppk2lab.capture.model import CaptureMeta

    monkeypatch.setattr(artifact_module, "CHUNK_SAMPLES", 1000)
    writer = ArtifactWriter(tmp_path / "cap.ppk2a", CaptureMeta())

    def boom(name, data):
        raise OSError("disk went away")

    monkeypatch.setattr(writer._zip, "writestr", boom)
    writer.add_block(_one_block(1000))
    with pytest.raises(OSError, match="disk went away"):
        # Either the next block or the finalize adopts it; both are the
        # caller's thread, and neither may return as if all was well.
        writer.add_block(_one_block(1000))
        writer.finalize(complete=True)


def test_chunk_table_stays_ordered_and_contiguous(tmp_path, monkeypatch):
    """Chunks are written off-thread; their table must still be in order."""
    from ppk2lab.capture.model import CaptureMeta

    monkeypatch.setattr(artifact_module, "CHUNK_SAMPLES", 1000)
    writer = ArtifactWriter(tmp_path / "cap.ppk2a", CaptureMeta())
    for i in range(5):
        writer.add_block(_one_block(1000, start_index=i * 1000))
    manifest = writer.finalize(complete=True)
    chunks = manifest["samples"]["chunks"]
    assert [c["file"] for c in chunks] == [f"chunks/{i:06d}.u32" for i in range(5)]
    assert [c["first_stored_index"] for c in chunks] == [0, 1000, 2000, 3000, 4000]
    assert ArtifactReader(tmp_path / "cap.ppk2a").stored_count == 5000


# -- provenance and in-capture actions -----------------------------------


def test_user_tags_travel_with_the_capture(tmp_path, capture):
    capture.meta.user_tags = {"sn": "POD01", "fw": "0.3.0", "scenario": "E12"}
    path = tmp_path / "tagged.ppk2a"
    capture.save(str(path))
    assert read_capture(path).meta.user_tags == {
        "sn": "POD01",
        "fw": "0.3.0",
        "scenario": "E12",
    }


@pytest.mark.parametrize(
    "tags",
    [
        {"sn": 3.7},
        {3: "x"},
        {"": "x"},
        {"k": "v" * 600},
        "not a mapping",
        {f"k{i}": "v" for i in range(100)},
    ],
)
def test_tags_that_are_not_short_text_are_refused(tags):
    from ppk2lab.capture.model import normalize_user_tags

    with pytest.raises(UsageError):
        normalize_user_tags(tags)


def test_a_number_is_not_quietly_stringified():
    """A tag reading "3.7" when 3.7 was passed is a lie about the record."""
    from ppk2lab.capture.model import normalize_user_tags

    with pytest.raises(UsageError, match="string"):
        normalize_user_tags({"voltage": 3.7})


def test_an_unusable_user_tags_block_is_refused(tmp_path, capture):
    path = tmp_path / "cap.ppk2a"
    capture.save(str(path))
    _rewrite_manifest(path, lambda m: m.update(user_tags={"sn": 7}))
    with pytest.raises(CaptureFileError, match="user_tags"):
        read_capture(path)


# -- back-pressure, and failures that must not pass silently -------------


def test_a_producer_that_outruns_the_disk_is_made_to_wait_rather_than_buffer(tmp_path, monkeypatch):
    """The queue depth is load-bearing in both directions.

    Unbounded, a disk that falls behind the instrument is paid for in RAM at
    4 MB a chunk with nothing to stop the growth, and a long capture dies of
    memory instead of slowing down. Bounded at one, the producer waits on
    every single chunk and the stall this arrangement exists to remove is
    back. Two lets it run a little ahead and no further.
    """
    import threading
    import time

    from ppk2lab.capture.model import CaptureMeta

    write_s = 0.3
    chunk = 100
    monkeypatch.setattr(artifact_module, "CHUNK_SAMPLES", chunk)
    writer = ArtifactWriter(tmp_path / "cap.ppk2a", CaptureMeta())

    writing = threading.Event()
    release = threading.Event()
    real_writestr = writer._zip.writestr

    def slow_writestr(name, data):
        if name.startswith("chunks/"):
            writing.set()
            release.wait(write_s)
        return real_writestr(name, data)

    monkeypatch.setattr(writer._zip, "writestr", slow_writestr)

    writer.add_block(_one_block(chunk))
    assert writing.wait(5.0), "the writer thread never took the first chunk"

    # The writer is now busy and the queue is empty, so what follows measures
    # the queue's capacity and nothing else.
    handover_s = []
    for i in range(1, 4):
        started = time.monotonic()
        writer.add_block(_one_block(chunk, start_index=i * chunk))
        handover_s.append(time.monotonic() - started)
    release.set()

    assert handover_s[0] < write_s / 3, f"the queue took only one chunk ({handover_s})"
    assert handover_s[1] < write_s / 3, f"the queue took only one chunk ({handover_s})"
    assert handover_s[2] > write_s / 3, f"the queue was never bounded ({handover_s})"

    # Waiting is not dropping: everything handed over is in the container.
    writer.finalize(complete=True)
    assert ArtifactReader(tmp_path / "cap.ppk2a").stored_count == 4 * chunk


def test_a_chunk_that_could_not_be_written_leaves_no_capture_and_no_debris(
    tmp_path, capture, monkeypatch
):
    """A manifest describes the chunk table it was told about, so a swallowed
    write failure produces a file that claims samples the container does not
    hold — and passes every integrity check the reader knows how to run,
    because the chunk it would have checked is simply not listed as missing.
    The failure has to reach the caller, and the half-written temp file has to
    go with it."""

    monkeypatch.setattr(artifact_module, "CHUNK_SAMPLES", 200)
    real_writestr = zipfile.ZipFile.writestr

    def refuse(self, name, data, *args, **kwargs):
        label = name if isinstance(name, str) else name.filename
        if label.startswith("chunks/"):
            raise OSError("disk went away")
        return real_writestr(self, name, data, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "writestr", refuse)

    path = tmp_path / "cap.ppk2a"
    with pytest.raises(OSError, match="disk went away"):
        write_capture(capture, path)
    monkeypatch.undo()
    assert list(tmp_path.iterdir()) == []


# -- artifacts written before these fields existed ------------------------
#
# `format_version` was deliberately not bumped for `user_tags`,
# `scheduled_actions` or warning categories: they are additions a 0.2.0 reader
# ignores, so refusing to open a 0.2.0 artifact over them would be the only
# breakage the change caused.


def test_a_manifest_without_the_new_provenance_blocks_still_opens(tmp_path, capture):
    """Every capture recorded by released 0.2.0 lacks both keys. Reading them
    with `[...]` instead of `.get(...)` would make each one unopenable."""

    def strip(manifest):
        del manifest["user_tags"]
        del manifest["scheduled_actions"]

    path = tmp_path / "cap.ppk2a"
    capture.save(str(path))
    _rewrite_manifest(path, strip)
    loaded = read_capture(path)
    assert loaded.meta.user_tags == {}
    assert loaded.meta.scheduled_actions == []


def test_a_stored_warning_written_before_categories_reads_back_with_one(tmp_path, capture):
    """A caller iterating `capture.warnings` must not have to handle two
    shapes in one list: the warnings the artifact carries and the ones reading
    it raised go through the same normalizer, and a code the catalog knows
    gets its category filled in rather than published as null."""
    path = tmp_path / "cap.ppk2a"
    capture.save(str(path))
    _rewrite_manifest(path, lambda m: [stored.pop("category", None) for stored in m["warnings"]])
    loaded = read_capture(path)
    assert loaded.warnings, "the fixture capture really does carry warnings"
    assert all(set(w) == {"code", "message", "category"} for w in loaded.warnings)
    categories = {w["code"]: w["category"] for w in loaded.warnings}
    assert categories["W_SAMPLE_GAPS"] == "capture integrity"


# -- durability ----------------------------------------------------------
#
# `os.replace` is atomic for the directory entry only. Without a sync the name
# can reach stable storage ahead of the bytes, and a power loss leaves a
# full-length `.ppk2a` that fails its own SHA-256 — a capture the operator was
# told had been written. Bench time is not re-acquirable; one sync is cheap
# against it. Durability itself cannot be tested from user space, so these pin
# the calls and their order.


def _fsync_events(monkeypatch):
    """Record every fsync and rename a write performs, in order.

    An fsync is identified by what it synced rather than by which descriptor
    number it got: `os.replace` preserves the inode, so the destination's
    inode afterwards names the handle that became this artifact.
    """
    events: list[tuple] = []
    real_fsync = os.fsync
    real_replace = os.replace

    def spy_fsync(fd):
        st = os.fstat(fd)
        events.append(("fsync", stat.S_ISDIR(st.st_mode), st.st_ino, st.st_size))
        return real_fsync(fd)

    def spy_replace(src, dst, **kwargs):
        events.append(("replace", src, dst))
        return real_replace(src, dst, **kwargs)

    monkeypatch.setattr(os, "fsync", spy_fsync)
    monkeypatch.setattr(os, "replace", spy_replace)
    return events


def test_the_capture_bytes_are_fsynced_before_the_capture_takes_its_name(
    tmp_path, capture, monkeypatch
):
    events = _fsync_events(monkeypatch)
    path = tmp_path / "cap.ppk2a"
    capture.save(str(path))

    inode = path.stat().st_ino
    synced = [i for i, e in enumerate(events) if e[0] == "fsync" and not e[1] and e[2] == inode]
    renamed = [i for i, e in enumerate(events) if e[0] == "replace"]
    assert synced, "the capture was renamed into place without being flushed"
    assert renamed and synced[0] < renamed[0]


@pytest.mark.skipif(os.name == "nt", reason="Windows has no directory handle to sync")
def test_the_directory_entry_is_fsynced_after_the_capture_is_renamed_into_place(
    tmp_path, capture, monkeypatch
):
    """Syncing the file is only half of it: the new name lives in the parent
    directory, and an unsynced directory can come back without it."""
    events = _fsync_events(monkeypatch)
    path = tmp_path / "cap.ppk2a"
    capture.save(str(path))

    parent = tmp_path.stat().st_ino
    synced = [i for i, e in enumerate(events) if e[0] == "fsync" and e[1] and e[2] == parent]
    renamed = [i for i, e in enumerate(events) if e[0] == "replace"]
    assert synced, "the directory entry was never flushed"
    assert renamed and synced[-1] > renamed[-1]


def test_the_whole_finished_artifact_is_flushed_not_only_what_was_written_so_far(
    tmp_path, capture, monkeypatch
):
    """The tail of a zip is its central directory, and it is written by
    `close()`. Syncing before that leaves the one structure without which the
    file is not a zip at all sitting in the page cache — so the loss the sync
    exists to prevent still happens, and lands on a capture that then cannot
    even be opened to be diagnosed."""
    events = _fsync_events(monkeypatch)
    path = tmp_path / "cap.ppk2a"
    capture.save(str(path))

    inode = path.stat().st_ino
    flushed = max(e[3] for e in events if e[0] == "fsync" and not e[1] and e[2] == inode)
    assert flushed == path.stat().st_size
