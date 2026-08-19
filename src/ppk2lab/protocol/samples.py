"""Raw 32-bit sample stream parsing with loss-aware framing.

Each sample is 4 bytes, little-endian ``uint32``:

===========  ==========================================
bits 0-13    14-bit ADC field (multiplied by 4 before calibration)
bits 14-16   current measurement range (valid 0-4)
bit 17       reserved / unused
bits 18-23   6-bit rolling sample counter (0-63)
bits 24-31   digital inputs; bit N is D<N>
===========  ==========================================

The parser:

- reassembles 4-byte frames across arbitrary chunk boundaries (1-3 byte
  remainders are kept between feeds);
- detects device-side sample loss through the 6-bit counter using modulo
  distance ``(actual - expected) & 0x3f``;
- accounts for host-side losses (dropped byte chunks) including realignment
  of the 4-byte framing after a drop whose size is not a multiple of 4;
- never shifts later samples earlier: the timeline index advances across
  every known gap, so timestamps stay truthful.

A loss of an exact multiple of 64 samples is invisible to the counter alone;
counter-derived gaps are therefore always marked ``ambiguous``.
"""

from __future__ import annotations

import sys
from array import array
from collections.abc import Iterator
from dataclasses import dataclass

from ..types import GapEvent

ADC_MASK = 0x3FFF
RANGE_SHIFT = 14
RANGE_MASK = 0x7
COUNTER_SHIFT = 18
COUNTER_MASK = 0x3F
LOGIC_SHIFT = 24
#: The 14-bit ADC field is multiplied by 4 before the calibration formula.
ADC_MULTIPLIER = 4
#: Ranges above this value are invalid; such samples are kept but flagged.
MAX_VALID_RANGE = 4


@dataclass(frozen=True)
class RawSample:
    """One decoded sample word, for tests and debugging."""

    word: int
    adc: int
    range_index: int
    counter: int
    logic: int

    @property
    def valid_range(self) -> bool:
        return self.range_index <= MAX_VALID_RANGE


def unpack_sample(word: int) -> RawSample:
    return RawSample(
        word=word,
        adc=word & ADC_MASK,
        range_index=(word >> RANGE_SHIFT) & RANGE_MASK,
        counter=(word >> COUNTER_SHIFT) & COUNTER_MASK,
        logic=(word >> LOGIC_SHIFT) & 0xFF,
    )


def pack_sample(adc: int, range_index: int, counter: int, logic: int) -> int:
    """Inverse of :func:`unpack_sample`, used by the simulator and tests."""
    if not 0 <= adc <= ADC_MASK:
        raise ValueError(f"adc out of range: {adc}")
    if not 0 <= range_index <= RANGE_MASK:
        raise ValueError(f"range out of range: {range_index}")
    if not 0 <= counter <= COUNTER_MASK:
        raise ValueError(f"counter out of range: {counter}")
    if not 0 <= logic <= 0xFF:
        raise ValueError(f"logic out of range: {logic}")
    return adc | (range_index << RANGE_SHIFT) | (counter << COUNTER_SHIFT) | (logic << LOGIC_SHIFT)


class SampleBlock:
    """A gap-free run of contiguous samples on the capture timeline."""

    __slots__ = ("_adc", "_counters", "_logic", "_ranges", "start_index", "words")

    def __init__(self, start_index: int, words: array) -> None:
        self.start_index = start_index
        self.words = words  # array('I'), host byte order already normalized
        self._adc: list[int] | None = None
        self._ranges: bytes | None = None
        self._counters: bytes | None = None
        self._logic: bytes | None = None

    def __len__(self) -> int:
        return len(self.words)

    @property
    def end_index(self) -> int:
        """Timeline index one past the last sample."""
        return self.start_index + len(self.words)

    @property
    def adc(self) -> list[int]:
        if self._adc is None:
            self._adc = [w & ADC_MASK for w in self.words]
        return self._adc

    @property
    def ranges(self) -> bytes:
        if self._ranges is None:
            self._ranges = bytes((w >> RANGE_SHIFT) & RANGE_MASK for w in self.words)
        return self._ranges

    @property
    def counters(self) -> bytes:
        if self._counters is None:
            self._counters = bytes((w >> COUNTER_SHIFT) & COUNTER_MASK for w in self.words)
        return self._counters

    @property
    def logic(self) -> bytes:
        if self._logic is None:
            self._logic = bytes((w >> LOGIC_SHIFT) & 0xFF for w in self.words)
        return self._logic

    @property
    def valid(self) -> list[bool]:
        return [r <= MAX_VALID_RANGE for r in self.ranges]

    def to_bytes(self) -> bytes:
        if sys.byteorder == "little":
            return self.words.tobytes()
        swapped = array("I", self.words)
        swapped.byteswap()
        return swapped.tobytes()

    def iter_samples(self) -> Iterator[RawSample]:
        for word in self.words:
            yield unpack_sample(word)


class SampleStreamParser:
    """Streaming parser: bytes in, ``SampleBlock``/``GapEvent`` out.

    Feed byte chunks of any size with :meth:`feed`; report host-side chunk
    drops with :meth:`notify_dropped_bytes`; report an unquantified stall
    with :meth:`notify_unknown_gap`.
    """

    def __init__(self, start_index: int = 0) -> None:
        self._buf = bytearray()
        self._skip_bytes = 0
        self._expected_counter: int | None = None
        self._next_index = start_index
        self.samples_emitted = 0
        self.gaps: list[GapEvent] = []
        #: True once an unknown-size gap occurred; timeline indexes after that
        #: point can no longer be mapped to wall-clock time.
        self.timeline_degraded = False

    @property
    def next_index(self) -> int:
        return self._next_index

    def _record_gap(self, missing: int | None, reason: str, ambiguous: bool) -> GapEvent:
        gap = GapEvent(index=self._next_index, missing=missing, reason=reason, ambiguous=ambiguous)
        self.gaps.append(gap)
        if missing is not None:
            self._next_index += missing
        else:
            self.timeline_degraded = True
        return gap

    def feed(self, data: bytes) -> list[SampleBlock | GapEvent]:
        """Parse a chunk; returns blocks and gap events in timeline order."""
        events: list[SampleBlock | GapEvent] = []
        if self._skip_bytes:
            take = min(self._skip_bytes, len(data))
            data = data[take:]
            self._skip_bytes -= take
            if self._skip_bytes:
                return events
        self._buf.extend(data)
        n_words = len(self._buf) // 4
        if n_words == 0:
            return events
        words = array("I")
        words.frombytes(bytes(self._buf[: n_words * 4]))
        del self._buf[: n_words * 4]
        if sys.byteorder == "big":
            words.byteswap()

        run_start = 0
        for i, word in enumerate(words):
            counter = (word >> COUNTER_SHIFT) & COUNTER_MASK
            expected = self._expected_counter
            if expected is not None and counter != expected:
                missing = (counter - expected) & COUNTER_MASK
                if i > run_start:
                    block = SampleBlock(self._next_index, words[run_start:i])
                    self._next_index += len(block)
                    self.samples_emitted += len(block)
                    events.append(block)
                events.append(self._record_gap(missing, "counter_skip", ambiguous=True))
                run_start = i
            self._expected_counter = (counter + 1) & COUNTER_MASK
        if len(words) > run_start:
            block = SampleBlock(self._next_index, words[run_start:])
            self._next_index += len(block)
            self.samples_emitted += len(block)
            events.append(block)
        return events

    def notify_dropped_bytes(self, dropped: int, reason: str = "host_overflow") -> GapEvent:
        """Account for ``dropped`` raw stream bytes lost on the host side.

        Computes the number of lost samples from the byte count (including a
        partial frame already buffered and a partial frame at the drop tail),
        realigns 4-byte framing, and advances the timeline. The event stays
        ``ambiguous`` because device-side counter gaps inside the dropped
        region are invisible; the counter check after the drop will surface
        any additional device-side loss as a separate gap.
        """
        if dropped <= 0:
            raise ValueError("dropped byte count must be positive")
        # The buffer always starts at a frame boundary, so the buffered
        # partial frame plus the dropped bytes form one contiguous lost run.
        lost_run = len(self._buf) + dropped
        self._buf.clear()
        missing = lost_run // 4
        tail = lost_run % 4
        if tail:
            # A frame was cut mid-way at the end of the dropped region; its
            # remaining bytes arrive next and must be skipped.
            missing += 1
            self._skip_bytes = 4 - tail
        if self._expected_counter is not None:
            self._expected_counter = (self._expected_counter + missing) & COUNTER_MASK
        return self._record_gap(missing, reason, ambiguous=True)

    def notify_unknown_gap(self, reason: str = "usb_stall") -> GapEvent:
        """Record a gap whose sample count cannot be determined.

        The timeline cannot advance truthfully, so it is marked degraded and
        the counter expectation resynchronizes on the next sample.
        """
        self._buf.clear()
        self._skip_bytes = 0
        self._expected_counter = None
        return self._record_gap(None, reason, ambiguous=True)

    def flush_stats(self) -> dict[str, object]:
        known_missing = sum(g.missing for g in self.gaps if g.missing is not None)
        return {
            "samples": self.samples_emitted,
            "gaps": len(self.gaps),
            "missing_samples_known": known_missing,
            "has_unknown_gaps": any(g.missing is None for g in self.gaps),
            "timeline_degraded": self.timeline_degraded,
        }
