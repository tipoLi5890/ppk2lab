"""Logic transition extraction with explicit gap handling.

A transition emitted with ``after_gap=True`` re-establishes the observed
state after missing data; any activity inside the gap is unknown and no
edge or pulse is ever claimed across it.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from ..protocol.samples import GapEvent, SampleBlock
from ..types import SAMPLE_PERIOD_S


@dataclass(frozen=True)
class Transition:
    sample_index: int
    changed_mask: int
    logic: int
    after_gap: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "sample_index": self.sample_index,
            "changed_mask": self.changed_mask,
            "logic": self.logic,
            "after_gap": self.after_gap,
        }


@dataclass(frozen=True)
class Edge:
    sample_index: int
    rising: bool
    after_gap: bool = False


@dataclass(frozen=True)
class Pulse:
    """A run at a constant level between two observed edges."""

    start_sample: int
    end_sample: int
    level: int
    valid: bool = True

    @property
    def width_samples(self) -> int:
        return self.end_sample - self.start_sample

    @property
    def width_s(self) -> float:
        return self.width_samples * SAMPLE_PERIOD_S

    def to_json(self) -> dict[str, Any]:
        return {
            "start_sample": self.start_sample,
            "end_sample": self.end_sample,
            "level": self.level,
            "width_samples": self.width_samples,
            "width_s": self.width_s,
            "valid": self.valid,
        }


def iter_transitions(
    events: Iterable[SampleBlock | GapEvent], channels_mask: int = 0xFF
) -> Iterator[Transition]:
    """Yield state changes on the selected channels.

    The first observed sample yields an initial transition with
    ``changed_mask == channels_mask``; so does the first sample after a gap
    (with ``after_gap=True``).
    """
    prev: int | None = None
    pending_gap = False
    for event in events:
        if isinstance(event, GapEvent):
            pending_gap = True
            prev = None
            continue
        logic = event.logic
        start = event.start_index
        for offset, byte in enumerate(logic):
            masked = byte & channels_mask
            if prev is None:
                yield Transition(start + offset, channels_mask, masked, after_gap=pending_gap)
                pending_gap = False
                prev = masked
            elif masked != prev:
                yield Transition(start + offset, masked ^ prev, masked)
                prev = masked


def edges(events: Iterable[SampleBlock | GapEvent], channel: int) -> list[Edge]:
    """Observed edges on one channel; edges are never claimed across gaps."""
    mask = 1 << channel
    out: list[Edge] = []
    prev: int | None = None
    for transition in iter_transitions(events, channels_mask=mask):
        level = 1 if transition.logic & mask else 0
        if transition.after_gap or prev is None:
            prev = level  # state re-established; not an edge
            continue
        if level != prev:
            out.append(Edge(transition.sample_index, rising=level == 1))
            prev = level
    return out


def pulses(events: Iterable[SampleBlock | GapEvent], channel: int) -> list[Pulse]:
    """Constant-level runs between consecutive observed edges.

    Runs adjacent to a gap or to the ends of the capture are excluded — a
    width bounded by missing data would be a false measurement.
    """
    mask = 1 << channel
    out: list[Pulse] = []
    run_start: int | None = None
    run_level: int | None = None
    run_bounded = False  # True once the run started at an observed edge
    for transition in iter_transitions(events, channels_mask=mask):
        level = 1 if transition.logic & mask else 0
        if transition.after_gap or run_level is None:
            run_start = transition.sample_index
            run_level = level
            run_bounded = False
            continue
        if level != run_level:
            if run_bounded and run_start is not None:
                out.append(Pulse(run_start, transition.sample_index, run_level))
            run_start = transition.sample_index
            run_level = level
            run_bounded = True
    return out
