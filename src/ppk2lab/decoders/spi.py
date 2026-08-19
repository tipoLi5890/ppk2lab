"""Streaming SPI decoder (modes 0-3, configurable CS, bit order, word size).

Edge-driven: words are latched on the mode's sampling edge of SCLK. State
never survives a sample gap — a word or transaction touching missing data is
closed with a ``gap`` error and zero confidence. Without a CS channel, words
are grouped into transactions by clock-idle timeout.
"""

from __future__ import annotations

from typing import Any

from ..errors import UsageError
from ..protocol.samples import GapEvent
from ..types import SAMPLE_RATE_HZ
from ..units import parse_channel
from .base import Annotation, LogicChunk, StreamingDecoder
from .feasibility import Feasibility, require_supported, spi_feasibility


def _word_confidence(cycle_samples: float | None, base: float) -> float:
    if cycle_samples is None:
        return base
    if cycle_samples >= 10:
        return base
    if cycle_samples >= 5:
        return min(base, 0.7)
    if cycle_samples >= 2.5:
        return min(base, 0.4)
    return 0.0


class SPIDecoder(StreamingDecoder):
    name = "spi"

    def __init__(
        self,
        sclk: str | int = "D1",
        mosi: str | int | None = None,
        miso: str | int | None = None,
        cs: str | int | None = None,
        *,
        mode: int = 0,
        word_bits: int = 8,
        msb_first: bool = True,
        cs_active_low: bool = True,
        expected_clock_hz: float | None = None,
        idle_timeout_samples: int | None = None,
        sample_rate: int = SAMPLE_RATE_HZ,
        allow_experimental: bool = False,
    ) -> None:
        super().__init__()
        if mode not in (0, 1, 2, 3):
            raise UsageError(f"SPI mode must be 0-3, got {mode}")
        if not 4 <= word_bits <= 32:
            raise UsageError(f"word_bits must be 4-32, got {word_bits}")
        if mosi is None and miso is None:
            raise UsageError("SPI decoder needs at least one of mosi/miso")

        def channel(value: str | int | None) -> int | None:
            if value is None:
                return None
            return value if isinstance(value, int) else parse_channel(value)

        sclk_bit = channel(sclk)
        if sclk_bit is None:
            raise UsageError("SPI decoder needs an sclk channel")
        self.sclk: int = sclk_bit
        self.mosi = channel(mosi)
        self.miso = channel(miso)
        self.cs = channel(cs)
        self.mode = mode
        self.cpol = mode >> 1
        self.cpha = mode & 1
        self.word_bits = word_bits
        self.msb_first = msb_first
        self.cs_active_low = cs_active_low
        self.sample_rate = sample_rate
        self.expected_clock_hz = expected_clock_hz
        self.feasibility: Feasibility | None = None
        if expected_clock_hz is not None:
            self.feasibility = require_supported(
                spi_feasibility(expected_clock_hz, sample_rate),
                allow_experimental=allow_experimental,
            )
        if idle_timeout_samples is None:
            if expected_clock_hz is not None:
                idle_timeout_samples = max(8, int(8 * sample_rate / expected_clock_hz))
            else:
                idle_timeout_samples = 1000
        self.idle_timeout_samples = idle_timeout_samples
        self._base_confidence = self.feasibility.confidence if self.feasibility else 1.0
        self._reset()

    # -- state -------------------------------------------------------------
    def _reset(self) -> None:
        self._prev_sclk: int | None = None
        self._prev_cs: int | None = None
        self._bit_count = 0
        self._mosi_val = 0
        self._miso_val = 0
        self._word_start: int | None = None
        self._word_min_cycle: float | None = None
        self._last_sample_edge: int | None = None
        self._txn_active = False
        self._txn_start: int | None = None
        self._txn_words: list[Annotation] = []
        self._txn_has_gap = False

    def describe(self) -> dict[str, Any]:
        def ch(bit: int | None) -> str | None:
            return None if bit is None else f"D{bit}"

        return {
            "decoder": self.name,
            "sclk": ch(self.sclk),
            "mosi": ch(self.mosi),
            "miso": ch(self.miso),
            "cs": ch(self.cs),
            "mode": self.mode,
            "word_bits": self.word_bits,
            "msb_first": self.msb_first,
            "cs_active_low": self.cs_active_low,
            "expected_clock_hz": self.expected_clock_hz,
            "idle_timeout_samples": self.idle_timeout_samples,
            "sample_rate_hz": self.sample_rate,
            "feasibility": self.feasibility.to_json() if self.feasibility else None,
        }

    # -- word / transaction management ------------------------------------
    def _close_word(self, end_index: int, error: str | None, out: list[Annotation]) -> None:
        if self._bit_count == 0:
            return
        errors = [error] if error else []
        if self._bit_count < self.word_bits:
            errors.append("partial_word")
        confidence = (
            0.0 if errors else _word_confidence(self._word_min_cycle, self._base_confidence)
        )
        fields: dict[str, Any] = {
            "bits": self._bit_count,
        }
        if self.mosi is not None:
            fields["mosi"] = self._mosi_val
            if self.word_bits <= 8:
                fields["mosi_hex"] = f"0x{self._mosi_val:02x}"
        if self.miso is not None:
            fields["miso"] = self._miso_val
            if self.word_bits <= 8:
                fields["miso_hex"] = f"0x{self._miso_val:02x}"
        if self._word_min_cycle is not None:
            fields["min_cycle_samples"] = self._word_min_cycle
        ann = Annotation(
            decoder=self.name,
            kind="word",
            start_sample=self._word_start if self._word_start is not None else end_index,
            end_sample=end_index,
            fields=fields,
            confidence=confidence,
            errors=errors,
        )
        out.append(ann)
        self._txn_words.append(ann)
        self._bit_count = 0
        self._mosi_val = 0
        self._miso_val = 0
        self._word_start = None
        self._word_min_cycle = None

    def _close_transaction(self, end_index: int, error: str | None, out: list[Annotation]) -> None:
        self._close_word(end_index, error, out)
        if not self._txn_active and not self._txn_words:
            return
        words = self._txn_words
        errors = [error] if error else []
        if self._txn_has_gap and "gap" not in errors:
            errors.append("gap")
        word_errors = any(w.errors for w in words)
        confidence = (
            0.0
            if (errors or word_errors)
            else min((w.confidence for w in words), default=self._base_confidence)
        )
        fields: dict[str, Any] = {"word_count": len(words)}
        if self.mosi is not None:
            fields["mosi_words"] = [w.fields.get("mosi") for w in words]
        if self.miso is not None:
            fields["miso_words"] = [w.fields.get("miso") for w in words]
        start = self._txn_start
        if start is None:
            start = words[0].start_sample if words else end_index
        out.append(
            Annotation(
                decoder=self.name,
                kind="transaction",
                start_sample=start,
                end_sample=end_index,
                fields=fields,
                confidence=confidence,
                errors=errors,
            )
        )
        self._txn_active = False
        self._txn_start = None
        self._txn_words = []
        self._txn_has_gap = False
        self._last_sample_edge = None

    # -- StreamingDecoder hooks -------------------------------------------
    def _feed(self, chunk: LogicChunk) -> list[Annotation]:
        out: list[Annotation] = []
        sclk_bit = self.sclk
        cs_bit = self.cs
        mosi_bit = self.mosi
        miso_bit = self.miso
        cs_active_level = 0 if self.cs_active_low else 1
        sample_on_leading = self.cpha == 0
        cpol = self.cpol

        for offset, byte in enumerate(chunk.logic):
            index = chunk.start_index + offset
            sclk = (byte >> sclk_bit) & 1
            cs = ((byte >> cs_bit) & 1) if cs_bit is not None else None

            if self._prev_sclk is None:
                # First sample after start/gap: levels observed, no edge claims.
                self._prev_sclk = sclk
                self._prev_cs = cs
                continue

            if cs_bit is not None and cs != self._prev_cs:
                if cs == cs_active_level:
                    if self._txn_active:
                        self._close_transaction(index, "reasserted", out)
                    self._txn_active = True
                    self._txn_start = index
                else:
                    self._close_transaction(index, None, out)
                self._prev_cs = cs

            in_transaction = self._txn_active if cs_bit is not None else True

            if (
                cs_bit is None
                and (self._txn_words or self._bit_count)
                and self._last_sample_edge is not None
                and index - self._last_sample_edge > self.idle_timeout_samples
            ):
                self._close_transaction(self._last_sample_edge + 1, None, out)

            if sclk != self._prev_sclk:
                leading = self._prev_sclk == cpol
                sampling_edge = leading if sample_on_leading else not leading
                if sampling_edge and in_transaction:
                    if self._last_sample_edge is not None:
                        cycle = float(index - self._last_sample_edge)
                        if self._word_min_cycle is None or cycle < self._word_min_cycle:
                            self._word_min_cycle = cycle
                    if self._bit_count == 0:
                        self._word_start = index
                        if cs_bit is None and not self._txn_active:
                            self._txn_active = True
                            self._txn_start = index
                    mo = (byte >> mosi_bit) & 1 if mosi_bit is not None else 0
                    mi = (byte >> miso_bit) & 1 if miso_bit is not None else 0
                    if self.msb_first:
                        self._mosi_val = (self._mosi_val << 1) | mo
                        self._miso_val = (self._miso_val << 1) | mi
                    else:
                        self._mosi_val |= mo << self._bit_count
                        self._miso_val |= mi << self._bit_count
                    self._bit_count += 1
                    self._last_sample_edge = index
                    if self._bit_count == self.word_bits:
                        self._close_word(index + 1, None, out)
                self._prev_sclk = sclk

        return out

    def _notify_gap(self, gap: GapEvent) -> list[Annotation]:
        out: list[Annotation] = []
        if self._txn_active or self._bit_count or self._txn_words:
            self._close_transaction(gap.index, "gap", out)
        self._prev_sclk = None
        self._prev_cs = None
        self._last_sample_edge = None
        return out

    def _flush(self) -> list[Annotation]:
        out: list[Annotation] = []
        if self._last_sample_edge is not None:
            end = self._last_sample_edge + 1
        elif self._txn_start is not None:
            end = self._txn_start
        else:
            end = 0
        if self._txn_active or self._bit_count or self._txn_words:
            self._close_transaction(end, "truncated", out)
        self._prev_sclk = None
        self._prev_cs = None
        return out
