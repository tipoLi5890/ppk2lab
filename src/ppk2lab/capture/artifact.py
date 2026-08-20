"""Canonical capture artifact (``.ppk2a``): chunked raw samples + manifest.

The artifact is a ZIP container:

- ``manifest.json``  — versioned manifest: identity, configuration, timeline,
  gap table, chunk table with CRC32s, SHA-256 of the raw sample bytes,
  completion state, and warnings;
- ``metadata.txt``   — raw device metadata text (calibration evidence);
- ``chunks/NNNNNN.u32`` — raw little-endian ``uint32`` sample words.

Raw samples are the source of truth; derived exports never replace them.
Writes are atomic (temp file + rename) and never overwrite an existing file
unless explicitly requested.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import zipfile
import zlib
from array import array
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ..diagnostics import W_MANIFEST_IMPLAUSIBLE, W_PARTIAL_INTEGRITY, Diagnostic, warn
from ..diagnostics import as_json as _warnings_as_json
from ..errors import CaptureFileError, CaptureTooLargeError, OutputExistsError, UsageError
from ..protocol.metadata import parse_metadata
from ..protocol.samples import GapEvent, SampleBlock
from ..types import SAMPLE_PERIOD_NS, SAMPLE_RATE_HZ
from .model import MAX_LOAD_SAMPLES, Capture, CaptureMeta

FORMAT_NAME = "ppk2lab-capture"
FORMAT_VERSION = 1
SAMPLE_ENCODING = "u32le-v1"
CHUNK_SAMPLES = 1_000_000
ARTIFACT_SUFFIX = ".ppk2a"

#: Keys of the manifest ``timeline`` block that describe the file's own index
#: space. Everything else in the block is the wall-clock cross-check recorded
#: while the stream ran, which belongs to the capture and must survive a
#: read/write round trip — a future timing key travels without a code change.
_TIMELINE_STRUCTURAL_KEYS = frozenset(
    {"sample_rate_hz", "sample_period_ns", "start_index", "degraded"}
)


__all__ = [
    "ARTIFACT_SUFFIX",
    "CHUNK_SAMPLES",
    "FORMAT_NAME",
    "FORMAT_VERSION",
    "MAX_LOAD_SAMPLES",
    "SAMPLE_ENCODING",
    "ArtifactReader",
    "ArtifactWriter",
    "read_capture",
    "read_window",
    "write_capture",
]


def _words_from_bytes(data: bytes) -> array:
    """Little-endian ``uint32`` sample words as stored, in host order."""
    words = array("I")
    words.frombytes(data)
    if sys.byteorder == "big":
        words.byteswap()
    return words


class ArtifactWriter:
    """Streaming, bounded-memory artifact writer with atomic finalize."""

    def __init__(self, path: str | os.PathLike[str], meta: CaptureMeta, *, overwrite: bool = False):
        self.path = Path(path)
        self.meta = meta
        self.overwrite = overwrite
        if self.path.exists():
            if not overwrite:
                raise OutputExistsError(f"output file exists: {self.path}")
            if not self.path.is_file():
                # Caught here, before a single sample is recorded: the rename
                # at the end would fail against a directory or a device node,
                # and by then the capture exists and cannot be re-acquired.
                raise UsageError(
                    f"output path exists and is not a regular file: {self.path}",
                    remediation="Pass a path to a file. --overwrite replaces a file, never a "
                    "directory or a device node.",
                )
        self._tmp_path = self.path.parent / f".{self.path.name}.tmp{os.getpid()}"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._zip = zipfile.ZipFile(self._tmp_path, "w", compression=zipfile.ZIP_DEFLATED)
        self._buffer = bytearray()
        self._chunks: list[dict[str, Any]] = []
        self._stored_count = 0
        self._invalid_count = 0
        self._gaps: list[GapEvent] = []
        self._gaps_truncated = 0
        self._sha256 = hashlib.sha256()
        self._start_index: int | None = None
        self._finalized = False

    def add_block(self, block: SampleBlock) -> None:
        if self._start_index is None:
            self._start_index = block.start_index
            self.meta.start_index = block.start_index
        data = block.to_bytes()
        self._buffer.extend(data)
        self._sha256.update(data)
        self._stored_count += len(block)
        self._invalid_count += sum(1 for r in block.ranges if r > 4)
        while len(self._buffer) >= CHUNK_SAMPLES * 4:
            self._flush_chunk(CHUNK_SAMPLES * 4)

    #: See CaptureBuilder.MAX_GAPS: the manifest records the count, not an
    #: unbounded enumeration.
    MAX_GAPS = 10_000

    def add_gap(self, gap: GapEvent) -> None:
        if len(self._gaps) < self.MAX_GAPS:
            self._gaps.append(gap)
        else:
            self._gaps_truncated += 1

    @property
    def gaps_truncated(self) -> int:
        """Gaps this writer counted but did not enumerate."""
        return self._gaps_truncated

    def _flush_chunk(self, n_bytes: int) -> None:
        data = bytes(self._buffer[:n_bytes])
        del self._buffer[:n_bytes]
        name = f"chunks/{len(self._chunks):06d}.u32"
        self._zip.writestr(name, data)
        self._chunks.append(
            {
                "file": name,
                "first_stored_index": sum(c["count"] for c in self._chunks),
                "count": n_bytes // 4,
                "crc32": zlib.crc32(data) & 0xFFFFFFFF,
            }
        )

    def finalize(
        self,
        *,
        complete: bool = True,
        interruption: dict[str, Any] | None = None,
        stats: dict[str, Any] | None = None,
        warnings: list[Any] | None = None,
        timeline_degraded: bool = False,
        calibration: dict[str, Any] | None = None,
        timing: dict[str, Any] | None = None,
        gaps_truncated: int = 0,
    ) -> dict[str, Any]:
        if self._finalized:
            raise RuntimeError("artifact already finalized")
        try:
            manifest = self._write_manifest(
                complete=complete,
                interruption=interruption,
                stats=stats,
                warnings=warnings,
                timeline_degraded=timeline_degraded,
                calibration=calibration,
                timing=timing,
                gaps_truncated=gaps_truncated,
            )
        except BaseException:
            # Nothing readable exists yet: the container has no manifest, so
            # the temp file is not a capture and keeping it helps nobody.
            self.abort()
            raise
        # Past this point the temp file IS a complete, valid, readable capture.
        # A failure now must never delete it: it holds bench time that cannot
        # be re-acquired. Mark the writer finalized so a caller's cleanup path
        # cannot remove it either, and name it in the remediation.
        self._finalized = True
        if self.path.exists() and not self.overwrite:
            # The destination appeared while the capture was running.
            raise OutputExistsError(
                f"output file exists: {self.path}",
                remediation=(
                    f"The complete capture is preserved at {self._tmp_path}. Move it to a "
                    "free path yourself, or re-run with --overwrite; the data is not lost."
                ),
            )
        try:
            os.replace(self._tmp_path, self.path)
        except OSError as exc:
            raise CaptureFileError(
                f"the capture was written but could not be moved to {self.path}: {exc}",
                remediation=(
                    f"The complete capture is preserved at {self._tmp_path} and is readable "
                    "as it stands. Move it to a valid destination yourself; do not re-run the "
                    "capture, the data is not lost."
                ),
            ) from exc
        return manifest

    def _write_manifest(
        self,
        *,
        complete: bool,
        interruption: dict[str, Any] | None,
        stats: dict[str, Any] | None,
        warnings: list[Any] | None,
        timeline_degraded: bool,
        calibration: dict[str, Any] | None,
        timing: dict[str, Any] | None,
        gaps_truncated: int,
    ) -> dict[str, Any]:
        if self._buffer:
            self._flush_chunk(len(self._buffer))
        gaps = sorted(self._gaps, key=lambda g: g.index)
        # A capture being rewritten arrives with its gap table already
        # truncated, so the count it carries is added rather than recomputed.
        truncated = self._gaps_truncated + gaps_truncated
        degraded = timeline_degraded or truncated > 0 or any(g.missing is None for g in gaps)
        manifest = {
            "format": FORMAT_NAME,
            "format_version": FORMAT_VERSION,
            "capture_id": self.meta.capture_id,
            "created_utc": self.meta.created_utc,
            "device": self.meta.device,
            "configuration": self.meta.configuration,
            "timeline": {
                "sample_rate_hz": self.meta.configuration.get("sample_rate_hz", SAMPLE_RATE_HZ),
                "sample_period_ns": SAMPLE_PERIOD_NS,
                "start_index": self.meta.start_index,
                "degraded": degraded,
                # Wall-clock cross-check: the only witness to sample loss the
                # 6-bit counter cannot describe (see capture/runner.py).
                **(timing or {}),
            },
            "calibration": calibration,
            "samples": {
                "encoding": SAMPLE_ENCODING,
                "stored_count": self._stored_count,
                "invalid_range_count": self._invalid_count,
                "sha256": self._sha256.hexdigest(),
                "chunks": self._chunks,
            },
            "gaps": [g.to_json() for g in gaps],
            "gaps_truncated": truncated,
            "complete": bool(complete and not gaps and not truncated),
            "interruption": interruption,
            "stats": stats,
            # Warnings are stored in their machine-readable form so a stored
            # capture stays as branchable as a live result.
            "warnings": _warnings_as_json(list(warnings or [])),
        }
        if self.meta.metadata_text is not None:
            self._zip.writestr("metadata.txt", self.meta.metadata_text)
        self._zip.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True))
        self._zip.close()
        return manifest

    def abort(self) -> None:
        if not self._finalized:
            try:
                self._zip.close()
            finally:
                self._tmp_path.unlink(missing_ok=True)


class ArtifactReader:
    """Reads an artifact with integrity checks and bounded-memory iteration."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        if not self.path.exists():
            raise CaptureFileError(f"capture file not found: {self.path}")
        if not self.path.is_file():
            # A directory reaches zipfile as an ordinary path and crashes it;
            # a FIFO blocks the read forever. Both are ordinary mistakes and
            # deserve an ordinary error rather than a hang or a traceback.
            raise CaptureFileError(f"not a regular file: {self.path}")
        try:
            self._zip = zipfile.ZipFile(self.path, "r")
            with self._zip.open("manifest.json") as fh:
                self.manifest: dict[str, Any] = json.load(fh)
        except (zipfile.BadZipFile, KeyError, OSError, json.JSONDecodeError) as exc:
            raise CaptureFileError(f"not a valid ppk2lab capture artifact: {self.path}") from exc
        if not isinstance(self.manifest, dict):
            raise CaptureFileError(f"capture manifest is not an object in {self.path}")
        if self.manifest.get("format") != FORMAT_NAME:
            raise CaptureFileError(f"unrecognized capture format in {self.path}")
        version = self.manifest.get("format_version")
        # `True` is an int in Python and would sail through as version 1.
        if isinstance(version, bool) or not isinstance(version, int):
            raise CaptureFileError(
                f"capture manifest has no usable format_version ({version!r}) in {self.path}"
            )
        if version > FORMAT_VERSION:
            raise CaptureFileError(
                f"capture format version {version} is newer than this ppk2lab supports "
                f"({FORMAT_VERSION})",
                remediation="Upgrade ppk2lab to a version that supports this format.",
            )
        samples = self.manifest.get("samples")
        if not isinstance(samples, dict):
            raise CaptureFileError(f"capture manifest has no samples block in {self.path}")
        if samples.get("encoding") != SAMPLE_ENCODING:
            raise CaptureFileError(
                f"unsupported sample encoding {samples.get('encoding')!r} in {self.path}"
            )
        stored = samples.get("stored_count")
        if isinstance(stored, bool) or not isinstance(stored, int) or stored < 0:
            raise CaptureFileError(
                f"capture manifest has no usable samples.stored_count ({stored!r}) in {self.path}"
            )
        self.stored_count: int = stored
        timeline = self.manifest.get("timeline")
        if not isinstance(timeline, dict):
            raise CaptureFileError(f"capture manifest has no timeline block in {self.path}")
        # Every time value derives from the sample rate, so a missing rate is
        # an error rather than an invitation to assume the usual one.
        rate = timeline.get("sample_rate_hz")
        if isinstance(rate, bool) or not isinstance(rate, int) or rate <= 0:
            raise CaptureFileError(
                f"capture manifest has no usable timeline.sample_rate_hz ({rate!r}) in "
                f"{self.path}; every timestamp depends on it"
            )
        self.sample_rate_hz: int = rate
        self._gaps, self.manifest_warnings = self._parse_gaps()

    def close(self) -> None:
        self._zip.close()

    def __enter__(self) -> ArtifactReader:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def gaps(self) -> list[GapEvent]:
        return list(self._gaps)

    @property
    def gaps_truncated(self) -> int:
        value = self.manifest.get("gaps_truncated", 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return 0
        return value

    @property
    def timing(self) -> dict[str, Any]:
        """The wall-clock cross-check stored with the capture, if any."""
        timeline = self.manifest.get("timeline", {})
        return {k: v for k, v in timeline.items() if k not in _TIMELINE_STRUCTURAL_KEYS}

    @property
    def start_index(self) -> int:
        """Timeline index of the first stored sample (nonzero when triggered)."""
        value = self.manifest.get("timeline", {}).get("start_index", 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return 0
        return value

    @property
    def missing_known(self) -> int:
        return sum(g.missing for g in self._gaps if g.missing is not None)

    @property
    def end_index(self) -> int:
        """Timeline index one past the last position the capture covers."""
        return self.start_index + self.stored_count + self.missing_known

    # -- timeline mapping, without materializing a sample ------------------
    #
    # The same walk :class:`~ppk2lab.capture.model.Capture` does, available
    # from the manifest alone. It is what lets a windowed read decide which
    # chunks to touch before touching any of them.

    def stored_before(self, timeline_index: int) -> tuple[int, GapEvent | None]:
        """``(stored samples before this position, the gap holding it)``.

        The gap is ``None`` unless the position falls inside one. An
        unknown-size gap occupies no timeline width, so it never holds a
        position — matching :meth:`Capture.timeline_to_stored`.
        """
        offset = 0
        for gap in self._gaps:
            missing = gap.missing or 0
            if timeline_index >= gap.index + missing:
                offset += missing
            elif timeline_index >= gap.index:
                return gap.index - self.start_index - offset, gap
            else:
                break
        return timeline_index - self.start_index - offset, None

    def timeline_of_stored(self, stored_index: int) -> int:
        """Timeline position of a stored sample."""
        t = self.start_index + stored_index
        for gap in self._gaps:
            missing = gap.missing or 0
            if gap.index <= t:
                t += missing
            else:
                break
        return t

    def _parse_gaps(self) -> tuple[list[GapEvent], list[Diagnostic]]:
        """Validate the stored gap table.

        Structure is an error: a table that cannot be read at all makes every
        timeline position downstream meaningless. Content that is merely
        impossible is a warning reported verbatim — clamping it into a
        plausible range would replace a manifest that is known wrong with a
        number that merely looks right.
        """
        raw = self.manifest.get("gaps", [])
        if not isinstance(raw, list):
            raise CaptureFileError(f"capture manifest gap table is not a list in {self.path}")
        gaps: list[GapEvent] = []
        for position, entry in enumerate(raw):
            where = f"gap {position} in {self.path}"
            if not isinstance(entry, dict):
                raise CaptureFileError(f"{where} is not an object")
            index = entry.get("index")
            if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                raise CaptureFileError(f"{where} has an unusable index ({index!r})")
            if "missing" not in entry:
                raise CaptureFileError(
                    f"{where} has no missing count; an absent count and a count of zero are "
                    "different claims and neither may be guessed"
                )
            missing = entry["missing"]
            if missing is not None and (
                isinstance(missing, bool) or not isinstance(missing, int) or missing < 0
            ):
                raise CaptureFileError(f"{where} has an unusable missing count ({missing!r})")
            gaps.append(
                GapEvent(
                    index=index,
                    missing=missing,
                    reason=str(entry.get("reason", "unknown")),
                    ambiguous=bool(entry.get("ambiguous", True)),
                )
            )

        warnings: list[Diagnostic] = []
        missing_known = sum(g.missing for g in gaps if g.missing is not None)
        start_index = self.manifest.get("timeline", {}).get("start_index", 0)
        if not isinstance(start_index, int) or isinstance(start_index, bool):
            start_index = 0
        # The writer legitimately records a gap sitting exactly on the end
        # bound, so only an index beyond it is impossible.
        end_bound = start_index + self.stored_count + missing_known
        beyond = [g.index for g in gaps if g.index > end_bound]
        if beyond:
            warnings.append(
                warn(
                    W_MANIFEST_IMPLAUSIBLE,
                    f"{len(beyond)} gap(s) are recorded past the end of the capture's timeline "
                    f"(first at index {beyond[0]:,}, timeline ends at {end_bound:,}); they are "
                    "reported as stored and cannot be placed",
                )
            )
        elapsed = self.timing.get("wall_elapsed_s")
        if isinstance(elapsed, int | float) and not isinstance(elapsed, bool) and elapsed > 0:
            # Nothing can go missing that the wall clock had no time to carry.
            # The 5% slack is the same delivery-jitter allowance the timeline
            # check already makes for its own anchoring.
            supportable = elapsed * self.sample_rate_hz * 1.05
            if missing_known > supportable:
                warnings.append(
                    warn(
                        W_MANIFEST_IMPLAUSIBLE,
                        f"the gap table claims {missing_known:,} missing samples, more than the "
                        f"{elapsed:g} s the capture ran could have produced "
                        f"(~{supportable:,.0f}); every duration derived from it is reported as "
                        "stored, not corrected",
                    )
                )
        return gaps, warnings

    def read_meta(self) -> CaptureMeta:
        metadata_text: str | None = None
        if "metadata.txt" in self._zip.namelist():
            metadata_text = self._zip.read("metadata.txt").decode("ascii", errors="replace")
        configuration = dict(self.manifest.get("configuration", {}))
        # The timeline block is authoritative for the rate the file was
        # recorded at; never fall back to a compiled-in default.
        configuration["sample_rate_hz"] = self.sample_rate_hz
        return CaptureMeta(
            capture_id=self.manifest.get("capture_id", ""),
            created_utc=self.manifest.get("created_utc", ""),
            device=self.manifest.get("device", {}),
            configuration=configuration,
            metadata_text=metadata_text,
            start_index=self.manifest.get("timeline", {}).get("start_index", 0),
        )

    def iter_chunk_bytes(
        self,
        *,
        verify: bool = True,
        stored_start: int = 0,
        stored_end: int | None = None,
    ) -> Iterator[tuple[int, bytes]]:
        """Yield ``(first_stored_index, raw bytes)`` for the chunks in range.

        ``stored_start``/``stored_end`` select a half-open range of *stored*
        indexes; chunks outside it are never read, which is what bounds the
        memory a windowed read needs by the window rather than by the file.

        The CRC32 is always checked over the whole chunk before any slicing:
        a per-chunk checksum is only a statement about the whole chunk, and
        checking a slice of it would be a different, weaker claim.
        """
        end = self.stored_count if stored_end is None else stored_end
        for chunk in self.manifest["samples"]["chunks"]:
            first = chunk["first_stored_index"]
            count = chunk["count"]
            if first + count <= stored_start or first >= end:
                continue
            data = self._zip.read(chunk["file"])
            if len(data) != count * 4:
                raise CaptureFileError(
                    f"chunk {chunk['file']} has {len(data)} bytes, expected {count * 4}"
                )
            if verify and (zlib.crc32(data) & 0xFFFFFFFF) != chunk["crc32"]:
                raise CaptureFileError(f"chunk {chunk['file']} failed its CRC32 check")
            lo = max(0, stored_start - first)
            hi = min(count, end - first)
            yield first + lo, data[lo * 4 : hi * 4]

    def iter_raw_words(
        self,
        *,
        verify: bool = True,
        stored_start: int = 0,
        stored_end: int | None = None,
    ) -> Iterator[tuple[int, array]]:
        """Yield ``(first_stored_index, words)`` per chunk, verifying CRCs."""
        for first, data in self.iter_chunk_bytes(
            verify=verify, stored_start=stored_start, stored_end=stored_end
        ):
            yield first, _words_from_bytes(data)

    def verify_sha256(self) -> bool:
        digest = hashlib.sha256()
        for _, data in self.iter_chunk_bytes(verify=False):
            digest.update(data)
        return digest.hexdigest() == self.manifest["samples"]["sha256"]


def write_capture(
    capture: Capture, path: str | os.PathLike[str], *, overwrite: bool = False
) -> str:
    writer = ArtifactWriter(path, capture.meta, overwrite=overwrite)
    try:
        for event in capture.iter_events():
            if isinstance(event, SampleBlock):
                writer.add_block(event)
            else:
                writer.add_gap(event)
        writer.finalize(
            complete=capture.complete,
            interruption=capture.interruption,
            stats=capture.summary() if capture.calibration else None,
            warnings=capture.warnings,
            timeline_degraded=capture.timeline_degraded,
            # The wall-clock witness cannot be recomputed from stored samples:
            # the loss it describes is precisely the loss they do not contain.
            timing=capture.timing,
            gaps_truncated=capture.gaps_truncated,
        )
    except BaseException:
        writer.abort()
        raise
    return str(writer.path)


def read_capture(
    path: str | os.PathLike[str], *, max_samples: int | None = MAX_LOAD_SAMPLES
) -> Capture:
    with ArtifactReader(path) as reader:
        stored = reader.stored_count
        if max_samples is not None and stored > max_samples:
            # Fixed h/GB formatting rendered a one-minute capture as
            # "0.0 h ... 0.0 GB of RAM", which reads as a refusal for no
            # reason. Scale both to the size actually being refused.
            seconds = stored / reader.sample_rate_hz
            span = (
                f"{seconds / 3600:.1f} h"
                if seconds >= 3600
                else f"{seconds / 60:.1f} min"
                if seconds >= 60
                else f"{seconds:.1f} s"
            )
            need = stored * 8
            memory = f"{need / 1e9:.1f} GB" if need >= 1e9 else f"{need / 1e6:.0f} MB"
            raise CaptureTooLargeError(
                f"capture holds {stored:,} samples ({span}); loading it whole would "
                f"need roughly {memory} of RAM, above the {max_samples:,}-sample ceiling"
            )
        meta = reader.read_meta()
        stored_warnings = _stored_warnings(reader)
        # The digest is folded in chunk by chunk as the words are read. Hashing
        # afterwards through Capture.sha256() would build a second full copy of
        # the samples, doubling the peak for a file that is already the largest
        # thing the process holds.
        digest = hashlib.sha256()
        words = array("I")
        for _, data in reader.iter_chunk_bytes():
            digest.update(data)
            words.extend(_words_from_bytes(data))
        if len(words) != stored:
            raise CaptureFileError(
                f"capture stores {len(words)} samples but the manifest declares {stored}"
            )
        recorded = reader.manifest["samples"]["sha256"]
        if digest.hexdigest() != recorded:
            raise CaptureFileError("capture sample data does not match its recorded SHA-256")
        capture = Capture(
            meta,
            words,
            reader.gaps,
            complete=bool(reader.manifest.get("complete", False)),
            interruption=reader.manifest.get("interruption"),
            warnings=stored_warnings,
            timeline_degraded=bool(reader.manifest.get("timeline", {}).get("degraded", False)),
            timing=reader.timing,
            gaps_truncated=reader.gaps_truncated,
            artifact_sha256=recorded,
        )
        # These words are the whole file's, and the digest over them was just
        # verified, so a later capture.sha256() must not pay for it again.
        capture._sha256 = recorded
        # Sanity: metadata text must still parse if present.
        if meta.metadata_text:
            parse_metadata(meta.metadata_text)
        return capture


def _stored_warnings(reader: ArtifactReader) -> list[Any]:
    """Warnings the artifact carries, plus anything reading it turned up."""
    stored = reader.manifest.get("warnings", [])
    if not isinstance(stored, list):
        stored = []
    return [*stored, *(w.to_json() for w in reader.manifest_warnings)]


def read_window(
    path: str | os.PathLike[str],
    *,
    start_index: int | None = None,
    end_index: int | None = None,
    start_s: float | None = None,
    end_s: float | None = None,
    max_samples: int | None = MAX_LOAD_SAMPLES,
) -> Capture:
    """Read one timeline window of a stored artifact.

    Bounds are timeline indexes, or seconds from the capture's own first
    sample; give at most one form per bound. An omitted bound is the capture's
    own. Only the chunks the window touches are read, so peak memory follows
    the window rather than the file — which is what makes an hours-long
    capture examinable at all.

    Three things this deliberately does not do:

    - It does not report the manifest's SHA-256. Only the chunks it read were
      checked, and only by their own CRC32; publishing the whole-file digest
      would be asserting a check that was not run. ``artifact_sha256`` is
      ``None`` and a :data:`W_PARTIAL_INTEGRITY` warning says why.
    - It does not drop ``gaps_truncated``. A capture whose gap table hit its
      ceiling cannot place every timeline position, and a window read out of
      it inherits that, so the count travels with the returned capture.
    - It does not clamp a window that starts inside a gap. There is no sample
      at that position to start from, and moving the start to the next one
      would silently answer a different question than the one asked.
    """
    with ArtifactReader(path) as reader:
        lo = _resolve_bound(reader, start_index, start_s, default=reader.start_index, name="start")
        hi = _resolve_bound(reader, end_index, end_s, default=reader.end_index, name="end")
        if hi <= lo:
            raise UsageError(
                f"window start {lo} is not before window end {hi}",
                remediation="Give an end bound greater than the start bound.",
            )
        stored_lo, gap = reader.stored_before(lo)
        if gap is not None:
            resume = gap.index + (gap.missing or 0)
            raise UsageError(
                f"window starts at timeline index {lo:,}, which is inside a {gap.missing:,}-sample "
                f"gap ({gap.reason}) beginning at {gap.index:,}; no sample exists there",
                remediation=(
                    f"Start at or before {gap.index:,}, or at or after {resume:,}. The window is "
                    "not moved for you: a shifted window answers a different question."
                ),
            )
        stored_hi, _ = reader.stored_before(hi)
        stored_lo = min(max(stored_lo, 0), reader.stored_count)
        stored_hi = min(max(stored_hi, stored_lo), reader.stored_count)
        count = stored_hi - stored_lo
        if max_samples is not None and count > max_samples:
            hours = count / reader.sample_rate_hz / 3600
            raise CaptureTooLargeError(
                f"the requested window holds {count:,} samples ({hours:.1f} h); loading it "
                f"would need roughly {count * 8 / 1e9:.1f} GB of RAM"
            )
        words = array("I")
        for _, chunk_words in reader.iter_raw_words(stored_start=stored_lo, stored_end=stored_hi):
            words.extend(chunk_words)

        meta = reader.read_meta()
        # The window's own first sample, not the file's: every timeline index
        # in the returned capture stays the one the artifact recorded.
        meta.start_index = reader.timeline_of_stored(stored_lo) if count else lo
        gaps = [g for g in reader.gaps if meta.start_index <= g.index < hi]
        warnings = _stored_warnings(reader)
        warnings.append(
            warn(
                W_PARTIAL_INTEGRITY,
                f"windowed read: the CRC32 of every chunk touched was verified, but the "
                f"manifest's SHA-256 over all {reader.stored_count:,} samples was not — this "
                "read never saw the rest of the file. Load the capture whole to check it",
            ).to_json()
        )
        capture = Capture(
            meta,
            words,
            gaps,
            complete=bool(reader.manifest.get("complete", False)),
            interruption=reader.manifest.get("interruption"),
            warnings=warnings,
            timeline_degraded=bool(reader.manifest.get("timeline", {}).get("degraded", False)),
            # Capture-level, and not recomputable from a slice: the loss the
            # wall clock witnessed is exactly what the stored samples lack.
            timing=reader.timing,
            gaps_truncated=reader.gaps_truncated,
            time_origin_index=reader.start_index,
            artifact_sha256=None,
        )
        if meta.metadata_text:
            parse_metadata(meta.metadata_text)
        return capture


def _resolve_bound(
    reader: ArtifactReader,
    index: int | None,
    seconds: float | None,
    *,
    default: int,
    name: str,
) -> int:
    """One window bound, given as a timeline index or as seconds."""
    if index is not None and seconds is not None:
        raise UsageError(
            f"give the window {name} as an index or as seconds, not both",
        )
    if index is not None:
        if isinstance(index, bool) or not isinstance(index, int):
            raise UsageError(f"window {name} index must be an integer, got {index!r}")
        return index
    if seconds is not None:
        if not isinstance(seconds, int | float) or not math.isfinite(seconds):
            raise UsageError(f"window {name} must be a finite number of seconds, got {seconds!r}")
        # Seconds are measured from the capture's own first sample, the same
        # origin `measure --window` and the exported time_s column use.
        return reader.start_index + round(seconds * reader.sample_rate_hz)
    return default
