"""In-memory capture model with a truthful, gap-preserving timeline.

Stored samples are the raw 32-bit words exactly as received. The timeline
index space includes missing samples: a gap advances the timeline without
storing data, so timestamps after a gap remain correct and later data is
never shifted earlier.
"""

from __future__ import annotations

import hashlib
import sys
import uuid
from array import array
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..calibration import Calibration, SpikeFilter
from ..errors import CalibrationUnavailableError
from ..protocol.metadata import parse_metadata
from ..protocol.samples import GapEvent, SampleBlock
from ..types import SAMPLE_PERIOD_S, SAMPLE_RATE_HZ

#: Loading an artifact materializes every sample plus a copy for hashing, so
#: the read path needs the same honesty as the write path: roughly 8 bytes of
#: peak RAM per stored sample. Defined here rather than beside the reader so
#: that :meth:`Capture.load` can name it without importing its own writer.
MAX_LOAD_SAMPLES = 200 * 1_000_000 // 8  # ~200 MB peak


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


@dataclass
class CaptureMeta:
    """Identity and configuration recorded with every capture."""

    capture_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_utc: str = field(default_factory=_utcnow_iso)
    device: dict[str, Any] = field(default_factory=dict)
    configuration: dict[str, Any] = field(default_factory=dict)
    #: Raw device metadata text (calibration evidence); None if unavailable.
    metadata_text: str | None = None
    #: Timeline index of the first stored sample (nonzero for triggered captures).
    start_index: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "capture_id": self.capture_id,
            "created_utc": self.created_utc,
            "device": dict(self.device),
            "configuration": dict(self.configuration),
            "start_index": self.start_index,
        }


class Capture:
    """A complete or partial capture: raw words, gaps, and metadata."""

    def __init__(
        self,
        meta: CaptureMeta,
        words: array,
        gaps: list[GapEvent],
        *,
        complete: bool = True,
        interruption: dict[str, Any] | None = None,
        warnings: list[Any] | None = None,
        timeline_degraded: bool = False,
        timing: dict[str, Any] | None = None,
        gaps_truncated: int = 0,
        time_origin_index: int | None = None,
        artifact_sha256: str | None = None,
    ) -> None:
        self.meta = meta
        self.words = words
        self.gaps = sorted(gaps, key=lambda g: g.index)
        self.complete = complete and not self.gaps
        self.interruption = interruption
        self.warnings = list(warnings or [])
        #: Gaps that occurred after the gap table hit its ceiling. The count is
        #: exact; the spans are not recoverable.
        self.gaps_truncated = gaps_truncated
        #: Wall-clock cross-check of the sample timeline, as recorded when the
        #: stream ran. It is the only witness to loss the 6-bit counter cannot
        #: describe, so it travels with the capture instead of being recomputed
        #: from stored data that no longer contains the evidence.
        self.timing: dict[str, Any] = dict(timing or {})
        # An unenumerated gap breaks the stored-to-timeline mapping just as an
        # unknown-size one does: positions after it cannot all be resolved.
        self.timeline_degraded = (
            timeline_degraded or gaps_truncated > 0 or any(g.missing is None for g in self.gaps)
        )
        #: Timeline index that reads as t = 0. Defaults to this capture's own
        #: first stored sample. A windowed read keeps the origin of the artifact
        #: it came from, so a window's timestamps line up with a full export's
        #: instead of restarting at zero and quietly answering a different
        #: question than the one the column name implies.
        self.time_origin_index = (
            meta.start_index if time_origin_index is None else time_origin_index
        )
        #: SHA-256 the source artifact records over its raw sample bytes, and
        #: only when this read actually verified it. ``None`` for an in-memory
        #: capture, which has no artifact, and for a windowed read, which
        #: verifies per-chunk CRC32s and never sees the whole file.
        self.artifact_sha256 = artifact_sha256
        self._calibration: Calibration | None = None
        self._sha256: str | None = None

    # -- basic properties --------------------------------------------------
    @property
    def stored_count(self) -> int:
        return len(self.words)

    @property
    def start_index(self) -> int:
        return self.meta.start_index

    @property
    def missing_known(self) -> int:
        return sum(g.missing for g in self.gaps if g.missing is not None)

    @property
    def end_index(self) -> int:
        """Timeline index one past the last stored sample."""
        return self.start_index + self.stored_count + self.missing_known

    @property
    def duration_s(self) -> float:
        return (self.end_index - self.start_index) * SAMPLE_PERIOD_S

    @property
    def sample_rate_hz(self) -> int:
        return int(self.meta.configuration.get("sample_rate_hz", SAMPLE_RATE_HZ))

    @property
    def calibration(self) -> Calibration | None:
        if self._calibration is None and self.meta.metadata_text:
            self._calibration = Calibration.from_metadata(parse_metadata(self.meta.metadata_text))
        return self._calibration

    @property
    def source_voltage_mv(self) -> int | None:
        value = self.meta.configuration.get("source_voltage_mv")
        if value is not None:
            return int(value)
        if self.calibration is not None:
            return self.calibration.vdd_mv
        return None

    def raw_bytes(self) -> bytes:
        if sys.byteorder == "little":
            return self.words.tobytes()
        swapped = array("I", self.words)
        swapped.byteswap()
        return swapped.tobytes()

    def sha256(self) -> str:
        if self._sha256 is None:
            self._sha256 = hashlib.sha256(self.raw_bytes()).hexdigest()
        return self._sha256

    # -- timeline mapping --------------------------------------------------
    def stored_to_timeline(self, stored_index: int) -> int:
        t = self.start_index + stored_index
        for gap in self.gaps:
            missing = gap.missing or 0
            if gap.index <= t:
                t += missing
            else:
                break
        return t

    def timeline_to_stored(self, timeline_index: int) -> int | None:
        """Stored index for a timeline position, or None inside a gap."""
        offset = 0
        for gap in self.gaps:
            missing = gap.missing or 0
            if timeline_index >= gap.index + missing:
                offset += missing
            elif timeline_index >= gap.index:
                return None
            else:
                break
        stored = timeline_index - self.start_index - offset
        if 0 <= stored < self.stored_count:
            return stored
        return None

    def time_to_index(self, seconds: float) -> int:
        return self.time_origin_index + round(seconds * self.sample_rate_hz)

    def index_to_time(self, timeline_index: int) -> float:
        return (timeline_index - self.time_origin_index) / self.sample_rate_hz

    # -- iteration ---------------------------------------------------------
    def iter_events(self, block_samples: int = 65536) -> Iterator[SampleBlock | GapEvent]:
        """Yield gap-free blocks and gap events in timeline order."""
        k = 0
        t = self.start_index
        gap_iter = iter(self.gaps)
        next_gap = next(gap_iter, None)
        total = self.stored_count
        while k < total or next_gap is not None:
            if next_gap is not None and next_gap.index == t:
                yield next_gap
                t += next_gap.missing or 0
                next_gap = next(gap_iter, None)
                continue
            run_end = total
            if next_gap is not None:
                run_end = min(run_end, k + (next_gap.index - t))
            if k >= run_end:
                # Defensive: inconsistent gap table would spin forever here.
                if next_gap is not None:
                    yield next_gap
                    t += next_gap.missing or 0
                    next_gap = next(gap_iter, None)
                    continue
                break
            n = min(block_samples, run_end - k)
            yield SampleBlock(t, self.words[k : k + n])
            k += n
            t += n

    # -- convenience accessors --------------------------------------------
    def logic_bytes(self) -> bytes:
        out = bytearray()
        for event in self.iter_events():
            if isinstance(event, SampleBlock):
                out.extend(event.logic)
        return bytes(out)

    def currents_ua(self, *, filtered: bool = False, vdd_mv: float | None = None) -> list[float]:
        """Calibrated current for every stored sample (NaN preserved)."""
        calibration = self.calibration
        if calibration is None:
            raise CalibrationUnavailableError("capture has no calibration metadata")
        vdd = vdd_mv if vdd_mv is not None else self.source_voltage_mv
        spike = SpikeFilter() if filtered else None
        out: list[float] = []
        for event in self.iter_events():
            if isinstance(event, GapEvent):
                if spike:
                    spike.notify_gap()
                continue
            values = calibration.convert_block(event, vdd)
            if spike:
                values = spike.apply(values, event.ranges)
            out.extend(values)
        return out

    def summary(self) -> dict[str, Any]:
        from .stats import compute_stats

        stats = compute_stats(self)
        return stats.to_json()

    # -- persistence --------------------------------------------------------
    def save(self, path: str, *, overwrite: bool = False) -> str:
        from .artifact import write_capture

        return write_capture(self, path, overwrite=overwrite)

    @classmethod
    def load(cls, path: str, *, max_samples: int | None = MAX_LOAD_SAMPLES) -> Capture:
        """Read a stored artifact whole.

        ``max_samples`` caps how much will be materialized; ``None`` removes
        the cap. See :func:`~ppk2lab.capture.artifact.read_capture`.
        """
        from .artifact import read_capture

        return read_capture(path, max_samples=max_samples)

    @classmethod
    def load_window(
        cls,
        path: str,
        *,
        start_index: int | None = None,
        end_index: int | None = None,
        start_s: float | None = None,
        end_s: float | None = None,
        max_samples: int | None = MAX_LOAD_SAMPLES,
    ) -> Capture:
        """Read one timeline window of a stored artifact.

        Peak memory follows the window, not the file, so an hours-long capture
        can be examined a slice at a time. The returned capture's
        ``artifact_sha256`` is ``None``: a windowed read verifies the CRC32 of
        the chunks it touches and never sees the whole file. See
        :func:`~ppk2lab.capture.artifact.read_window`.
        """
        from .artifact import read_window

        return read_window(
            path,
            start_index=start_index,
            end_index=end_index,
            start_s=start_s,
            end_s=end_s,
            max_samples=max_samples,
        )


class CaptureBuilder:
    """Accumulates stream events into a Capture."""

    def __init__(self, meta: CaptureMeta) -> None:
        self.meta = meta
        self.words = array("I")
        self.gaps: list[GapEvent] = []
        self.warnings: list[Any] = []
        self.timeline_degraded = False
        self.gaps_truncated = 0
        self._first_index: int | None = None

    #: A desync storm can produce gaps far faster than anyone can read them;
    #: past this the count is still exact, only the enumeration stops.
    MAX_GAPS = 10_000

    def add(self, event: SampleBlock | GapEvent) -> None:
        if isinstance(event, SampleBlock):
            if self._first_index is None:
                self._first_index = event.start_index
                self.meta.start_index = event.start_index
            self.words.extend(event.words)
        else:
            if len(self.gaps) < self.MAX_GAPS:
                self.gaps.append(event)
            else:
                self.gaps_truncated += 1
            if event.missing is None:
                self.timeline_degraded = True

    def finish(
        self,
        *,
        complete: bool = True,
        interruption: dict[str, Any] | None = None,
        timing: dict[str, Any] | None = None,
    ) -> Capture:
        return Capture(
            self.meta,
            self.words,
            self.gaps,
            complete=complete,
            interruption=interruption,
            warnings=self.warnings,
            timeline_degraded=self.timeline_degraded,
            timing=timing,
            gaps_truncated=self.gaps_truncated,
        )
