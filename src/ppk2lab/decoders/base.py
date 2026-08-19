"""Streaming decoder contract.

Decoders are stateful stream consumers: they accept contiguous logic chunks
in timeline order, must be told about every sample gap, and must be flushed
at end of stream. Every annotation refers back to exact capture sample
ranges; a frame or transaction that touches missing data is reported with a
``gap`` error and zero confidence — never as a confident decode.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any

from ..errors import UsageError
from ..protocol.samples import GapEvent, SampleBlock


@dataclass(frozen=True)
class LogicChunk:
    """A gap-free run of logic bytes starting at a timeline sample index."""

    start_index: int
    logic: bytes

    @property
    def end_index(self) -> int:
        return self.start_index + len(self.logic)


@dataclass
class Annotation:
    """One decoded event, tied to an exact sample range."""

    decoder: str
    kind: str
    start_sample: int
    end_sample: int
    fields: dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0
    errors: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "decoder": self.decoder,
            "kind": self.kind,
            "start_sample": self.start_sample,
            "end_sample": self.end_sample,
            "fields": dict(self.fields),
            "confidence": self.confidence,
            "errors": list(self.errors),
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Annotation:
        return cls(
            decoder=data["decoder"],
            kind=data["kind"],
            start_sample=int(data["start_sample"]),
            end_sample=int(data["end_sample"]),
            fields=dict(data.get("fields", {})),
            confidence=float(data.get("confidence", 1.0)),
            errors=list(data.get("errors", [])),
        )


class StreamingDecoder(abc.ABC):
    """Base class enforcing contiguity between feeds.

    Subclasses implement ``_feed``, ``_notify_gap``, ``_flush``; the public
    methods track the expected next sample index and convert unannounced
    discontinuities into explicit gap notifications.
    """

    name = "decoder"

    def __init__(self) -> None:
        self._expected_index: int | None = None

    # -- public API --------------------------------------------------------
    def feed(self, chunk: LogicChunk) -> list[Annotation]:
        annotations: list[Annotation] = []
        if self._expected_index is not None:
            if chunk.start_index < self._expected_index:
                raise UsageError(
                    f"decoder fed overlapping data: chunk starts at {chunk.start_index}, "
                    f"expected {self._expected_index}"
                )
            if chunk.start_index > self._expected_index:
                implicit = GapEvent(
                    index=self._expected_index,
                    missing=chunk.start_index - self._expected_index,
                    reason="discontinuous_feed",
                    ambiguous=False,
                )
                annotations.extend(self._notify_gap(implicit))
        self._expected_index = chunk.end_index
        annotations.extend(self._feed(chunk))
        return annotations

    def notify_gap(self, gap: GapEvent) -> list[Annotation]:
        if gap.missing is not None:
            self._expected_index = gap.index + gap.missing
        else:
            self._expected_index = None  # timeline unknown after the gap
        return self._notify_gap(gap)

    def flush(self) -> list[Annotation]:
        return self._flush()

    def reset(self) -> None:
        self._expected_index = None
        self._reset()

    # -- subclass hooks ----------------------------------------------------
    @abc.abstractmethod
    def _feed(self, chunk: LogicChunk) -> list[Annotation]: ...

    @abc.abstractmethod
    def _notify_gap(self, gap: GapEvent) -> list[Annotation]: ...

    @abc.abstractmethod
    def _flush(self) -> list[Annotation]: ...

    @abc.abstractmethod
    def _reset(self) -> None: ...

    @abc.abstractmethod
    def describe(self) -> dict[str, Any]:
        """Machine-readable decoder configuration for manifests and results."""


def decode_capture(capture: Any, decoder: StreamingDecoder) -> list[Annotation]:
    """Run a streaming decoder over a capture's events, gap-aware."""
    annotations: list[Annotation] = []
    for event in capture.iter_events():
        if isinstance(event, SampleBlock):
            annotations.extend(decoder.feed(LogicChunk(event.start_index, event.logic)))
        else:
            annotations.extend(decoder.notify_gap(event))
    annotations.extend(decoder.flush())
    return annotations
