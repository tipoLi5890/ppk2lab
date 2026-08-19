"""Streaming UART decoder with fractional-phase center sampling.

The decoder waits for an observed idle level before arming, locates start
edges, and samples every bit at its nominal center ``start + (k + 0.5) *
samples_per_bit`` so non-integer sample-per-bit ratios (9600 baud at
100 kS/s is 10.417) do not accumulate phase error. Frames that touch a
sample gap or the end of the stream are reported as errors with zero
confidence, never as decoded data.
"""

from __future__ import annotations

from typing import Any

from ..errors import UsageError
from ..protocol.samples import GapEvent
from ..types import SAMPLE_RATE_HZ
from ..units import parse_channel
from .base import Annotation, LogicChunk, StreamingDecoder
from .feasibility import Feasibility, require_supported, uart_feasibility


class UARTDecoder(StreamingDecoder):
    name = "uart"

    def __init__(
        self,
        rx: str | int = "D0",
        baud: int = 9600,
        *,
        data_bits: int = 8,
        parity: str | None = None,
        stop_bits: int = 1,
        msb_first: bool = False,
        invert: bool = False,
        sample_rate: int = SAMPLE_RATE_HZ,
        allow_experimental: bool = False,
    ) -> None:
        super().__init__()
        if not 5 <= data_bits <= 9:
            raise UsageError(f"data_bits must be 5-9, got {data_bits}")
        if parity not in (None, "even", "odd"):
            raise UsageError(f"parity must be None, 'even', or 'odd', got {parity!r}")
        if stop_bits not in (1, 2):
            raise UsageError(f"stop_bits must be 1 or 2, got {stop_bits}")
        self.channel = rx if isinstance(rx, int) else parse_channel(rx)
        self.baud = baud
        self.data_bits = data_bits
        self.parity = parity
        self.stop_bits = stop_bits
        self.msb_first = msb_first
        self.invert = invert
        self.sample_rate = sample_rate
        self.feasibility: Feasibility = require_supported(
            uart_feasibility(baud, sample_rate), allow_experimental=allow_experimental
        )
        self._spb = sample_rate / baud
        self._frame_bits = 1 + data_bits + (1 if parity else 0) + stop_bits
        #: relative center-sample offset of bit k from the start edge
        self._bit_offset = [round((k + 0.5) * self._spb) for k in range(self._frame_bits)]
        self._frame_span = round(self._frame_bits * self._spb)
        self._need = self._bit_offset[-1] + 1
        #: samples of continuous idle required before arming start-edge
        #: detection at stream start and after a gap. 1.5 bit times rejects
        #: lone high data bits, so the decoder cannot fabricate bytes by
        #: locking onto the middle of a frame that a gap cut open.
        self._arm_idle = max(2, round(1.5 * self._spb))
        self._reset()

    # -- state -------------------------------------------------------------
    def _reset(self) -> None:
        self._bits = bytearray()
        self._base = 0
        self._armed = False
        self._in_break = False
        self._break_start = 0

    def describe(self) -> dict[str, Any]:
        return {
            "decoder": self.name,
            "rx": f"D{self.channel}",
            "baud": self.baud,
            "data_bits": self.data_bits,
            "parity": self.parity,
            "stop_bits": self.stop_bits,
            "msb_first": self.msb_first,
            "invert": self.invert,
            "sample_rate_hz": self.sample_rate,
            "feasibility": self.feasibility.to_json(),
        }

    # -- helpers -----------------------------------------------------------
    def _trim(self, keep_from: int) -> None:
        if keep_from > 0:
            del self._bits[:keep_from]
            self._base += keep_from

    def _frame_annotation(self, s: int) -> Annotation:
        bits = self._bits
        offsets = self._bit_offset
        sampled = [bits[s + off] for off in offsets]
        data = sampled[1 : 1 + self.data_bits]
        errors: list[str] = []
        value = 0
        if self.msb_first:
            for bit in data:
                value = (value << 1) | bit
        else:
            for k, bit in enumerate(data):
                value |= bit << k
        cursor = 1 + self.data_bits
        if self.parity is not None:
            parity_bit = sampled[cursor]
            cursor += 1
            expected = sum(data) % 2 if self.parity == "even" else 1 - sum(data) % 2
            if parity_bit != expected:
                errors.append("parity")
        if any(bit != 1 for bit in sampled[cursor:]):
            errors.append("framing")
        fields: dict[str, Any] = {"value": value, "channel": f"D{self.channel}"}
        if self.data_bits <= 8:
            fields["byte"] = f"0x{value:02x}"
            fields["char"] = chr(value) if 32 <= value < 127 else None
        return Annotation(
            decoder=self.name,
            kind="frame",
            start_sample=self._base + s,
            end_sample=self._base + s + self._frame_span,
            fields=fields,
            confidence=self.feasibility.confidence,
            errors=errors,
        )

    def _process(self, *, final: bool = False, end_reason: str | None = None) -> list[Annotation]:
        anns: list[Annotation] = []
        bits = self._bits
        pos = 0
        while True:
            n = len(bits)
            if self._in_break:
                high = bits.find(b"\x01", pos)
                if high < 0:
                    if final:
                        anns.append(
                            Annotation(
                                self.name,
                                "break",
                                self._break_start,
                                self._base + n,
                                {"channel": f"D{self.channel}"},
                                confidence=0.0,
                                errors=[end_reason or "truncated"],
                            )
                        )
                        self._in_break = False
                        self._trim(n)
                    else:
                        self._trim(n)  # break continues into the next chunk
                    return anns
                anns.append(
                    Annotation(
                        self.name,
                        "break",
                        self._break_start,
                        self._base + high,
                        {"channel": f"D{self.channel}"},
                        confidence=self.feasibility.confidence,
                    )
                )
                self._in_break = False
                self._armed = False  # require idle again after a break
                pos = high
                continue
            if not self._armed:
                # arm only after a continuous idle run of >= self._arm_idle
                run_start = None
                armed_at = -1
                i = pos
                while i < n:
                    if bits[i] == 1:
                        if run_start is None:
                            run_start = i
                        if i - run_start + 1 >= self._arm_idle:
                            armed_at = i
                            break
                    else:
                        run_start = None
                    i += 1
                if armed_at < 0:
                    # keep the trailing idle run so it can complete next feed
                    keep_from = run_start if run_start is not None else n
                    self._trim(keep_from)
                    return anns
                self._armed = True
                pos = armed_at
                continue
            # locate a start (falling) edge
            edge = -1
            for i in range(max(pos, 1), n):
                if bits[i - 1] == 1 and bits[i] == 0:
                    edge = i
                    break
            if edge < 0:
                if n > 0:
                    self._trim(n - 1)  # keep one sample of history for edge detection
                return anns
            if n - edge < self._need:
                if final:
                    anns.append(
                        Annotation(
                            self.name,
                            "error",
                            self._base + edge,
                            self._base + n,
                            {"reason": "incomplete_frame", "channel": f"D{self.channel}"},
                            confidence=0.0,
                            errors=[end_reason or "truncated"],
                        )
                    )
                    self._trim(n)
                else:
                    self._trim(edge - 1)
                return anns
            if bits[edge + self._bit_offset[0]] != 0:
                # start bit did not hold to its center: glitch, not a frame
                pos = edge + 1
                continue
            sampled = [bits[edge + off] for off in self._bit_offset]
            if not any(sampled):
                # everything low including stops: line break
                self._in_break = True
                self._break_start = self._base + edge
                pos = edge + self._bit_offset[-1]
                continue
            anns.append(self._frame_annotation(edge))
            pos = edge + self._bit_offset[-1] + 1

    # -- StreamingDecoder hooks -------------------------------------------
    def _feed(self, chunk: LogicChunk) -> list[Annotation]:
        if not self._bits:
            self._base = chunk.start_index
        mask = 1 << self.channel
        if self.invert:
            self._bits.extend(0 if (b & mask) else 1 for b in chunk.logic)
        else:
            self._bits.extend(1 if (b & mask) else 0 for b in chunk.logic)
        return self._process()

    def _notify_gap(self, gap: GapEvent) -> list[Annotation]:
        anns = self._process(final=True, end_reason="gap")
        self._bits.clear()
        self._armed = False
        self._in_break = False
        if gap.missing is not None:
            self._base = gap.index + gap.missing
        return anns

    def _flush(self) -> list[Annotation]:
        anns = self._process(final=True, end_reason="truncated")
        self._bits.clear()
        self._armed = False
        self._in_break = False
        return anns


def uart_bytes(annotations: list[Annotation]) -> tuple[bytes, list[int]]:
    """Reconstruct the clean byte stream from frame annotations.

    Returns ``(data, end_samples)`` where ``end_samples[i]`` is the timeline
    end of byte ``i``. Frames with errors are excluded — corrupted data never
    silently joins the stream.
    """
    data = bytearray()
    ends: list[int] = []
    for ann in annotations:
        if ann.kind == "frame" and not ann.errors and ann.fields.get("value", 256) < 256:
            data.append(ann.fields["value"])
            ends.append(ann.end_sample)
    return bytes(data), ends
