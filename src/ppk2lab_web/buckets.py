"""Live decimation into 1 ms buckets, and the console's distribution grid.

This is a port of ``webui/src/core/decimator.ts``, not a second decimation
design. The console draws directly from the typed arrays a ``Bucket`` fills, so
the two have to agree bucket for bucket; a golden fixture drives the same
samples through both and compares. Where the TypeScript has a comment
explaining why a line is the way it is, the comment travels with the line.

Only tier 0 is produced here. The console's ``Decimator`` folds ten of each
tier into the next as buckets arrive, so sending anything coarser would send
the same data twice.

What this adds to the port is ``excluded``. The console's ``Bucket`` has two
bins -- ``n`` for samples that arrived and ``gap`` for samples known to be
missing -- and a sample whose current cannot be trusted is neither. It *was*
delivered, so calling it a gap accuses the instrument of loss it did not
commit; it is *not* usable, so putting it in ``n`` poisons the mean. It gets a
bin of its own, and ``n + gap + excluded`` is always ``SAMPLES_PER_BUCKET`` for
a closed bucket. On a calibrated device the column is identically zero.
"""

from __future__ import annotations

import math
from array import array
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ppk2lab.capture.stats import IMPLAUSIBLE_CURRENT_UA, QUANTILE_MIN_UA
from ppk2lab.protocol.samples import MAX_VALID_RANGE
from ppk2lab.types import SAMPLE_RATE_HZ

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

#: Tier-0 bucket width. `webui/src/core/constants.ts` derives the same number
#: from `TIERS[0].ms`; it is 1 ms at the device's fixed 100 kS/s.
BUCKET_MS = 1
SAMPLES_PER_BUCKET = (SAMPLE_RATE_HZ * BUCKET_MS) // 1000
BUCKET_SECONDS = BUCKET_MS / 1000.0

#: Saturating width of the edge counter, matching the console's Uint16Array.
EDGES_MAX = 65535

# -- the console's distribution grid ----------------------------------------
#
# Deliberately *not* `capture/stats.py`'s grid. That one is 128 bins per decade
# over the instrument's whole span; this one is 24 over seven decades, 169 bins
# including the underflow bin. Both are log-spaced with the same 200 nA floor
# and the same "bin 0 is an upper bound" meaning, but a quantile read from this
# grid is coarser than the same quantile from `ppk2lab measure`, and that
# difference is documented rather than smoothed over: the console is a live
# display, and shipping 858 float64 bins several times a second to say so more
# precisely would not make the picture any truer.
HIST_DECADES = 7
HIST_PER_DECADE = 24
HIST_BINS = HIST_DECADES * HIST_PER_DECADE


def new_histogram() -> array[float]:
    """A zeroed grid, ``HIST_BINS + 1`` wide. Bin 0 is the underflow bin."""
    return array("d", bytes(8 * (HIST_BINS + 1)))


def hist_add(hist: array[float], ua: float) -> None:
    """Place one current in the grid.

    Written as ``not (ua > FLOOR)`` rather than ``ua <= FLOOR`` so that NaN
    lands in bin 0, exactly as the JavaScript ``!(uA > QUANTILE_MIN_UA)`` does.
    Spelled the other way the two implementations would disagree on NaN, which
    is the value a device with missing calibration constants produces most.
    """
    if not (ua > QUANTILE_MIN_UA):
        hist[0] += 1.0
        return
    decade = math.log10(ua / QUANTILE_MIN_UA)
    # min(), not a wider array: the top bin is also the overflow bin, as in the
    # console. Porting the clamp matters more than improving it.
    hist[min(HIST_BINS, 1 + math.floor(decade * HIST_PER_DECADE))] += 1.0


def hist_bin_value(index: int) -> float:
    """The representative current for a bin: its geometric midpoint."""
    if index == 0:
        return QUANTILE_MIN_UA
    return QUANTILE_MIN_UA * 10.0 ** ((index - 0.5) / HIST_PER_DECADE)


@dataclass(slots=True)
class Bucket:
    """One closed 1 ms bucket.

    Field for field the console's ``Bucket``, plus ``excluded`` and the
    absolute ``start_index`` that lets a client place it on the timeline
    without trusting a float.
    """

    #: Sample index of the first sample this bucket covers.
    start_index: int
    #: Session-relative seconds of that sample.
    t0: float
    #: Microamps. Meaningless when ``n == 0``; emitted as 0.0, as in the console.
    min_ua: float
    max_ua: float
    #: Sum of usable currents, microamps. The mean is ``sum_ua / n``.
    sum_ua: float
    #: Usable samples.
    n: int
    #: Samples known to be missing.
    gap: int
    #: Samples delivered but not usable -- see the module docstring.
    excluded: int
    #: Bit r set when measurement range r appeared, including invalid ranges.
    range_mask: int
    #: OR / AND of the D0-D7 bytes seen.
    logic_any: int
    logic_all: int
    #: Bit transitions counted, saturating at EDGES_MAX.
    edges: int


class Tier0Accumulator:
    """Turns blocks and gaps into closed 1 ms buckets.

    One instance per session. It owns the counters the console displays --
    ``Decimator.ingestBucket`` deliberately does not maintain them, because one
    loss spans many buckets and counting gaps per bucket would report the
    bucket count instead.
    """

    def __init__(self, *, start_index: int = 0) -> None:
        self.histogram = new_histogram()
        self.total_stored = 0
        self.total_missing = 0
        self.total_excluded = 0
        self.gap_count = 0
        #: True once a gap arrived whose size could not be established. The
        #: timeline cannot be advanced truthfully past one.
        self.timeline_degraded = False

        self._start_index = start_index
        self._filled = 0
        self._last_logic = -1
        self._reset_accumulator()

    # -- accumulator ------------------------------------------------------
    def _reset_accumulator(self) -> None:
        self._min = math.inf
        self._max = -math.inf
        self._sum = 0.0
        self._n = 0
        self._gap = 0
        self._excluded = 0
        self._range_mask = 0
        self._logic_any = 0
        self._logic_all = 0xFF
        self._edges = 0

    def _emit(self) -> Bucket:
        index = self._start_index
        bucket = Bucket(
            start_index=index,
            t0=index / SAMPLE_RATE_HZ,
            min_ua=self._min if self._n else 0.0,
            max_ua=self._max if self._n else 0.0,
            sum_ua=self._sum,
            n=self._n,
            gap=self._gap,
            excluded=self._excluded,
            range_mask=self._range_mask,
            logic_any=self._logic_any,
            # An empty bucket has no "all channels high" to report; 0xff would
            # claim it did.
            logic_all=self._logic_all if self._n else 0,
            edges=self._edges,
        )
        # Advance by what was actually placed, not by a full width. At a normal
        # close those are the same; at a flush the bucket is short, and the next
        # one has to start where this one really ended or every later index --
        # and so every later timestamp -- is wrong.
        self._start_index = index + self._filled
        self._filled = 0
        self._reset_accumulator()
        return bucket

    # -- ingestion --------------------------------------------------------
    def add_samples(
        self,
        currents_ua: Sequence[float],
        ranges: bytes,
        logic: bytes,
    ) -> list[Bucket]:
        """Feed one block's worth of converted samples.

        ``currents_ua`` comes from :meth:`ppk2lab.calibration.Calibration.convert_block`
        and is in microamps, with ``nan`` wherever a range has no constants or
        the source voltage is unknown. The three sequences are the same length
        and in the same order.
        """
        closed: list[Bucket] = []
        last_logic = self._last_logic
        for ua, r, bits in zip(currents_ua, ranges, logic, strict=True):
            self._range_mask |= 1 << r
            self._logic_any |= bits
            self._logic_all &= bits
            if last_logic >= 0:
                edges = self._edges + (last_logic ^ bits).bit_count()
                self._edges = edges if edges < EDGES_MAX else EDGES_MAX
            last_logic = bits

            if r > MAX_VALID_RANGE or ua != ua or abs(ua) > IMPLAUSIBLE_CURRENT_UA:
                # Delivered, and not usable. Its range field and its logic byte
                # were real, so they are kept above; only the current is not.
                self._excluded += 1
                self.total_excluded += 1
            else:
                if ua < self._min:
                    self._min = ua
                if ua > self._max:
                    self._max = ua
                self._sum += ua
                self._n += 1
                self.total_stored += 1
                hist_add(self.histogram, ua)

            self._filled += 1
            if self._filled >= SAMPLES_PER_BUCKET:
                closed.append(self._emit())
        self._last_logic = last_logic
        return closed

    def add_gap(self, missing: int | None) -> list[Bucket]:
        """Record samples that never arrived.

        ``None`` means the size could not be established. Nothing is filled --
        advancing the timeline by a guess would move every later sample -- and
        the session is marked degraded so the console can say the timeline is
        no longer trustworthy rather than drawing a shorter capture.
        """
        # `not (missing > 0)` covers None and 0 alike, matching the console's
        # `if (!(missing > 0))`.
        if missing is None or missing <= 0:
            if missing is None:
                self.timeline_degraded = True
            # The line was not observed across the hole, so the next sample
            # starts no edge.
            self._last_logic = -1
            return []

        self.total_missing += missing
        self.gap_count += 1
        closed: list[Bucket] = []
        left = missing
        while left > 0:
            take = min(SAMPLES_PER_BUCKET - self._filled, left)
            self._gap += take
            self._filled += take
            left -= take
            if self._filled >= SAMPLES_PER_BUCKET:
                closed.append(self._emit())
        self._last_logic = -1
        return closed

    def flush(self) -> list[Bucket]:
        """Close a partially filled bucket at a discontinuity.

        A stream stop or an applied mode change ends the run of samples the
        current bucket was collecting. Emitting it short keeps the samples that
        were taken; carrying it across the change would blend two different
        device states into one bucket, and dropping it would lose them.
        """
        if self._filled == 0:
            self._last_logic = -1
            return []
        bucket = self._emit()
        self._last_logic = -1
        return [bucket]

    @property
    def next_index(self) -> int:
        """Sample index the next closed bucket will start at."""
        return self._start_index

    @property
    def position(self) -> int:
        """Sample index the next sample will occupy.

        This accumulator is the console's timeline, so this is where an event
        lands on it -- which is not always what the event itself says. A
        recording opens its own stream whose indices restart at zero, and
        reporting those would put a gap somewhere the console never was.
        """
        return self._start_index + self._filled

    def break_continuity(self) -> None:
        """Start no edge across the next sample, without closing a bucket."""
        self._last_logic = -1

    def covered_fraction(self) -> float:
        expected = self.total_stored + self.total_missing
        return self.total_stored / expected if expected else 1.0
