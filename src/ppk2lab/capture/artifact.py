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
import os
import sys
import zipfile
import zlib
from array import array
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ..errors import CaptureFileError, OutputExistsError
from ..protocol.metadata import parse_metadata
from ..protocol.samples import GapEvent, SampleBlock
from ..types import SAMPLE_PERIOD_NS, SAMPLE_RATE_HZ
from .model import Capture, CaptureMeta

FORMAT_NAME = "ppk2lab-capture"
FORMAT_VERSION = 1
SAMPLE_ENCODING = "u32le-v1"
CHUNK_SAMPLES = 1_000_000
ARTIFACT_SUFFIX = ".ppk2a"


class ArtifactWriter:
    """Streaming, bounded-memory artifact writer with atomic finalize."""

    def __init__(self, path: str | os.PathLike[str], meta: CaptureMeta, *, overwrite: bool = False):
        self.path = Path(path)
        self.meta = meta
        self.overwrite = overwrite
        if self.path.exists() and not overwrite:
            raise OutputExistsError(f"output file exists: {self.path}")
        self._tmp_path = self.path.parent / f".{self.path.name}.tmp{os.getpid()}"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._zip = zipfile.ZipFile(self._tmp_path, "w", compression=zipfile.ZIP_DEFLATED)
        self._buffer = bytearray()
        self._chunks: list[dict[str, Any]] = []
        self._stored_count = 0
        self._invalid_count = 0
        self._gaps: list[GapEvent] = []
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

    def add_gap(self, gap: GapEvent) -> None:
        self._gaps.append(gap)

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
        warnings: list[str] | None = None,
        timeline_degraded: bool = False,
    ) -> dict[str, Any]:
        if self._finalized:
            raise RuntimeError("artifact already finalized")
        if self._buffer:
            self._flush_chunk(len(self._buffer))
        gaps = sorted(self._gaps, key=lambda g: g.index)
        degraded = timeline_degraded or any(g.missing is None for g in gaps)
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
            },
            "samples": {
                "encoding": SAMPLE_ENCODING,
                "stored_count": self._stored_count,
                "invalid_range_count": self._invalid_count,
                "sha256": self._sha256.hexdigest(),
                "chunks": self._chunks,
            },
            "gaps": [g.to_json() for g in gaps],
            "complete": bool(complete and not gaps),
            "interruption": interruption,
            "stats": stats,
            "warnings": list(warnings or []),
        }
        if self.meta.metadata_text is not None:
            self._zip.writestr("metadata.txt", self.meta.metadata_text)
        self._zip.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True))
        self._zip.close()
        if self.path.exists() and not self.overwrite:
            self._tmp_path.unlink(missing_ok=True)
            raise OutputExistsError(f"output file exists: {self.path}")
        os.replace(self._tmp_path, self.path)
        self._finalized = True
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
        try:
            self._zip = zipfile.ZipFile(self.path, "r")
            with self._zip.open("manifest.json") as fh:
                self.manifest: dict[str, Any] = json.load(fh)
        except (zipfile.BadZipFile, KeyError, json.JSONDecodeError) as exc:
            raise CaptureFileError(f"not a valid ppk2lab capture artifact: {self.path}") from exc
        if self.manifest.get("format") != FORMAT_NAME:
            raise CaptureFileError(f"unrecognized capture format in {self.path}")
        if int(self.manifest.get("format_version", -1)) > FORMAT_VERSION:
            raise CaptureFileError(
                f"capture format version {self.manifest.get('format_version')} is newer "
                f"than this ppk2lab supports ({FORMAT_VERSION})",
                remediation="Upgrade ppk2lab to a version that supports this format.",
            )
        encoding = self.manifest.get("samples", {}).get("encoding")
        if encoding != SAMPLE_ENCODING:
            raise CaptureFileError(f"unsupported sample encoding {encoding!r} in {self.path}")

    def close(self) -> None:
        self._zip.close()

    def __enter__(self) -> ArtifactReader:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def gaps(self) -> list[GapEvent]:
        return [
            GapEvent(
                index=g["index"],
                missing=g["missing"],
                reason=g.get("reason", "unknown"),
                ambiguous=bool(g.get("ambiguous", True)),
            )
            for g in self.manifest.get("gaps", [])
        ]

    def read_meta(self) -> CaptureMeta:
        metadata_text: str | None = None
        if "metadata.txt" in self._zip.namelist():
            metadata_text = self._zip.read("metadata.txt").decode("ascii", errors="replace")
        return CaptureMeta(
            capture_id=self.manifest.get("capture_id", ""),
            created_utc=self.manifest.get("created_utc", ""),
            device=self.manifest.get("device", {}),
            configuration=self.manifest.get("configuration", {}),
            metadata_text=metadata_text,
            start_index=self.manifest.get("timeline", {}).get("start_index", 0),
        )

    def iter_raw_words(self, *, verify: bool = True) -> Iterator[tuple[int, array]]:
        """Yield ``(first_stored_index, words)`` per chunk, verifying CRCs."""
        for chunk in self.manifest["samples"]["chunks"]:
            data = self._zip.read(chunk["file"])
            if len(data) != chunk["count"] * 4:
                raise CaptureFileError(
                    f"chunk {chunk['file']} has {len(data)} bytes, expected {chunk['count'] * 4}"
                )
            if verify and (zlib.crc32(data) & 0xFFFFFFFF) != chunk["crc32"]:
                raise CaptureFileError(f"chunk {chunk['file']} failed its CRC32 check")
            words = array("I")
            words.frombytes(data)
            if sys.byteorder == "big":
                words.byteswap()
            yield chunk["first_stored_index"], words

    def verify_sha256(self) -> bool:
        digest = hashlib.sha256()
        for chunk in self.manifest["samples"]["chunks"]:
            digest.update(self._zip.read(chunk["file"]))
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
        )
    except BaseException:
        writer.abort()
        raise
    return str(writer.path)


def read_capture(path: str | os.PathLike[str]) -> Capture:
    with ArtifactReader(path) as reader:
        meta = reader.read_meta()
        words = array("I")
        for _, chunk_words in reader.iter_raw_words():
            words.extend(chunk_words)
        expected = reader.manifest["samples"]["stored_count"]
        if len(words) != expected:
            raise CaptureFileError(
                f"capture stores {len(words)} samples but the manifest declares {expected}"
            )
        capture = Capture(
            meta,
            words,
            reader.gaps,
            complete=bool(reader.manifest.get("complete", False)),
            interruption=reader.manifest.get("interruption"),
            warnings=list(reader.manifest.get("warnings", [])),
            timeline_degraded=bool(reader.manifest.get("timeline", {}).get("degraded", False)),
        )
        recorded = reader.manifest["samples"]["sha256"]
        if capture.sha256() != recorded:
            raise CaptureFileError("capture sample data does not match its recorded SHA-256")
        # Sanity: metadata text must still parse if present.
        if meta.metadata_text:
            parse_metadata(meta.metadata_text)
        return capture
