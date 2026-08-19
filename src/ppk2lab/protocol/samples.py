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


#: Consecutive mismatching counters that prove the byte framing is lost.
#: A device-side skip mismatches exactly once and then counts correctly again;
#: a shifted stream reads its counter out of neighbouring fields, which are
#: either constant (logic byte, ADC high bits) or noise — both mismatch on
#: essentially every word. Four is the smallest value three unrelated device
#: skips landing on adjacent samples cannot reach.
DESYNC_CONSECUTIVE_MISMATCHES = 4
#: Sliding-window backstop for a desync that happens not to mismatch on every
#: word: this many mismatches within the last :data:`DESYNC_WINDOW_BITS`.
DESYNC_WINDOW_BITS = 16
DESYNC_WINDOW_MISMATCHES = 8
_RECENT_MASK = (1 << DESYNC_WINDOW_BITS) - 1
#: Correctly-incrementing samples that must follow a mismatch before its gap
#: is committed and the timeline advanced. Strictly below
#: :data:`DESYNC_CONSECUTIVE_MISMATCHES`, so a desync verdict always wins the
#: race and no gap derived from a shifted counter is ever committed.
SKIP_CONFIRM_WORDS = 3
#: Never hold a pending gap longer than this; past it the loss is reported
#: from the aggregate counter distance rather than withheld indefinitely.
PENDING_MAX_WORDS = 8
#: Bytes gathered before scoring the four possible framing offsets.
RESYNC_SCORE_BYTES = 1024
#: Minimum counter-continuity score (out of 127) for a candidate byte offset
#: to be adopted. Below it, no framing is recoverable from that window and
#: adopting the "best" one only produces another desync a few words later.
RESYNC_MIN_SCORE = 64
#: Upper bound on the stored gap table; a desync storm must not grow memory
#: without limit. Losses beyond it are still counted, just not enumerated.
MAX_GAP_EVENTS = 10_000


def _swapped(words: array) -> bytes:
    """Serialize words back to little-endian wire order on big-endian hosts."""
    copy = array("I", words)
    copy.byteswap()
    return copy.tobytes()


def _alignment_score(data: bytes, offset: int, max_words: int = 128) -> int:
    """Rate a candidate byte offset by counter continuity.

    The correct framing makes the 6-bit counter increment by one from word to
    word; a wrong offset scatters it. Scoring all four offsets recovers the
    stream after a partial byte loss instead of discarding everything after
    it.
    """
    usable = (len(data) - offset) // 4
    if usable <= 1:
        return -1
    words = array("I")
    words.frombytes(data[offset : offset + min(usable, max_words) * 4])
    if sys.byteorder == "big":
        words.byteswap()
    score = 0
    prev: int | None = None
    for word in words:
        counter = (word >> COUNTER_SHIFT) & COUNTER_MASK
        if prev is not None and counter == ((prev + 1) & COUNTER_MASK):
            score += 1
        prev = counter
    return score


class SampleStreamParser:
    """Streaming parser: bytes in, ``SampleBlock``/``GapEvent`` out.

    Feed byte chunks of any size with :meth:`feed`; report host-side chunk
    drops with :meth:`notify_dropped_bytes`; report an unquantified stall
    with :meth:`notify_unknown_gap`.

    A counter mismatch alone is not proof of device-side loss: byte-level
    framing loss (a partial byte run vanished, so every later word is
    shifted) produces the same symptom on nearly every word. A mismatch is
    therefore held until the following samples confirm the framing survived;
    only then does its gap commit and advance the timeline. If instead the
    mismatches keep coming, the parser records one ``stream_desync`` event,
    discards the shifted words rather than passing them off as measurements,
    and re-aligns by scoring the four candidate byte offsets.
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
        #: Gaps that occurred after the table hit :data:`MAX_GAP_EVENTS`.
        self.gaps_truncated = 0
        #: Number of framing desyncs detected and re-aligned.
        self.desync_events = 0
        #: Bytes discarded because no byte offset made the counter continuous.
        self.resync_bytes_discarded = 0
        self._consec_mismatch = 0
        self._recent = 0
        self._resync_buf: bytearray | None = None

    @property
    def next_index(self) -> int:
        return self._next_index

    def _record_gap(self, missing: int | None, reason: str, ambiguous: bool) -> GapEvent:
        gap = GapEvent(index=self._next_index, missing=missing, reason=reason, ambiguous=ambiguous)
        if len(self.gaps) < MAX_GAP_EVENTS:
            self.gaps.append(gap)
        else:
            self.gaps_truncated += 1
        if missing is not None:
            self._next_index += missing
        else:
            self.timeline_degraded = True
        return gap

    def _note_counter_result(self, mismatch: bool) -> bool:
        """Track framing health; True once the byte framing looks lost.

        Two O(1) integer signals, deliberately not an averaged rate: a device
        skip mismatches once and then counts correctly, so neither signal
        moves, while a byte-level shift mismatches on almost every word and
        trips the consecutive counter within four samples — fast enough that
        no gap derived from a shifted counter is ever committed.
        """
        self._recent = ((self._recent << 1) | int(mismatch)) & _RECENT_MASK
        if mismatch:
            self._consec_mismatch += 1
        else:
            self._consec_mismatch = 0
        return (
            self._consec_mismatch >= DESYNC_CONSECUTIVE_MISMATCHES
            or self._recent.bit_count() >= DESYNC_WINDOW_MISMATCHES
        )

    def _emit_block(
        self, events: list[SampleBlock | GapEvent], words: array, start: int, stop: int
    ) -> None:
        if stop <= start:
            return
        block = SampleBlock(self._next_index, words[start:stop])
        self._next_index += len(block)
        self.samples_emitted += len(block)
        events.append(block)

    def _enter_resync(self, carry: bytes = b"") -> GapEvent:
        self.desync_events += 1
        self._skip_bytes = 0
        self._expected_counter = None
        self._consec_mismatch = 0
        self._recent = 0
        # The unparsed remainder belongs *after* the carried tail; splicing
        # two non-adjacent byte runs would defeat the offset scoring.
        buf = bytearray(carry)
        buf.extend(self._buf)
        self._buf.clear()
        self._resync_buf = buf
        return self._record_gap(None, "stream_desync", ambiguous=True)

    def _try_resync(self, data: bytes) -> bool:
        """Accumulate bytes and adopt the best framing offset. True when done."""
        assert self._resync_buf is not None
        self._resync_buf.extend(data)
        while len(self._resync_buf) >= RESYNC_SCORE_BYTES:
            candidate = bytes(self._resync_buf)
            scores = [_alignment_score(candidate, off) for off in range(4)]
            best = max(range(4), key=scores.__getitem__)
            if scores[best] >= RESYNC_MIN_SCORE:
                self._resync_buf = None
                self._buf.extend(candidate[best:])
                return True
            # No offset makes the counter continuous: these bytes carry no
            # recoverable framing. Drop them (the stream_desync already marked
            # the timeline degraded) instead of re-desyncing word by word.
            self.resync_bytes_discarded += len(candidate) - 3
            del self._resync_buf[: len(candidate) - 3]
        return False

    def feed(self, data: bytes) -> list[SampleBlock | GapEvent]:
        """Parse a chunk; returns blocks and gap events in timeline order."""
        events: list[SampleBlock | GapEvent] = []
        if self._resync_buf is not None:
            if not self._try_resync(data):
                return events
            data = b""
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
        pending = -1  # word index of an unconfirmed mismatch, -1 = none
        pending_expected = 0  # the counter that word should have carried
        confirm = 0  # consecutive correctly-incrementing words since `pending`

        for i, word in enumerate(words):
            counter = (word >> COUNTER_SHIFT) & COUNTER_MASK
            expected = self._expected_counter
            mismatch = expected is not None and counter != expected
            self._expected_counter = (counter + 1) & COUNTER_MASK

            if mismatch:
                confirm = 0
                if pending < 0:
                    # `mismatch` implies `expected is not None`.
                    pending, pending_expected = i, expected or 0
            elif pending >= 0:
                confirm += 1

            # A healthy stream never touches the detector.
            if (mismatch or self._recent) and self._note_counter_result(mismatch):
                # The framing is lost, so the pending mismatch was never a
                # device-side skip. Drop it, and cut the emitted block at the
                # FIRST mismatch so no shifted word reaches a consumer as if
                # it were a measurement. The timeline does not advance.
                cut = pending if pending >= 0 else i
                self._emit_block(events, words, run_start, cut)
                tail = words[i + 1 :]
                carry = tail.tobytes() if sys.byteorder == "little" else _swapped(tail)
                events.append(self._enter_resync(carry))
                if self._try_resync(b""):
                    events.extend(self.feed(b""))
                return events

            if pending >= 0 and (
                confirm >= SKIP_CONFIRM_WORDS or i - pending + 1 >= PENDING_MAX_WORDS
            ):
                # The framing survived the mismatch, so the skip was the
                # device's. Report the loss over the whole held span.
                missing = (counter - pending_expected - (i - pending)) & COUNTER_MASK
                self._emit_block(events, words, run_start, pending)
                events.append(self._record_gap(missing, "counter_skip", ambiguous=True))
                run_start, pending, confirm = pending, -1, 0

        if pending >= 0 and self._expected_counter is not None:
            # The chunk ended before confirmation could arrive. Report it now:
            # a withheld gap the caller can never learn about would hide data
            # loss, and with no desync verdict the counter is still the best
            # evidence available. This is the only path that can advance the
            # timeline on an unconfirmed mismatch, and the consecutive-mismatch
            # verdict bounds how often it can happen.
            last = len(words) - 1
            counter = (self._expected_counter - 1) & COUNTER_MASK
            missing = (counter - pending_expected - (last - pending)) & COUNTER_MASK
            self._emit_block(events, words, run_start, pending)
            events.append(self._record_gap(missing, "counter_skip", ambiguous=True))
            run_start = pending

        self._emit_block(events, words, run_start, len(words))
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
        # A host-side drop breaks sample adjacency but not byte framing.
        self._consec_mismatch = 0
        return self._record_gap(missing, reason, ambiguous=True)

    def notify_unknown_gap(self, reason: str = "usb_stall") -> GapEvent:
        """Record a gap whose sample count cannot be determined.

        The timeline cannot advance truthfully, so it is marked degraded and
        the counter expectation resynchronizes on the next sample.
        """
        self._buf.clear()
        self._skip_bytes = 0
        self._expected_counter = None
        self._consec_mismatch = 0
        self._recent = 0
        return self._record_gap(None, reason, ambiguous=True)

    def flush_stats(self) -> dict[str, object]:
        known_missing = sum(g.missing for g in self.gaps if g.missing is not None)
        return {
            "samples": self.samples_emitted,
            "gaps": len(self.gaps),
            "gaps_truncated": self.gaps_truncated,
            "desync_events": self.desync_events,
            "resync_bytes_discarded": self.resync_bytes_discarded,
            "missing_samples_known": known_missing,
            "has_unknown_gaps": any(g.missing is None for g in self.gaps),
            "timeline_degraded": self.timeline_degraded,
        }
