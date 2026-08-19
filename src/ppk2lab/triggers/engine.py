"""Trigger detectors and the pre/post-trigger capture engine.

The engine transforms a live event stream into the triggered capture window
``[fire - pre_samples, fire + post_samples)`` while preserving gap events
that overlap the window. Detectors report the timeline index at which their
condition fired.
"""

from __future__ import annotations

import abc
import re
from collections import deque
from typing import Any

from ..calibration import Calibration
from ..decoders.base import LogicChunk
from ..decoders.spi import SPIDecoder
from ..decoders.uart import UARTDecoder
from ..errors import UsageError
from ..protocol.samples import GapEvent, SampleBlock
from ..units import parse_channel, parse_current_ua


class TriggerDetector(abc.ABC):
    requires_current = False

    @abc.abstractmethod
    def feed(self, block: SampleBlock, currents_ua: list[float] | None) -> int | None:
        """Return the timeline index where the trigger fired, or None."""

    def on_gap(self, gap: GapEvent) -> None:  # noqa: B027 - optional hook
        pass

    @abc.abstractmethod
    def describe(self) -> dict[str, Any]: ...


class CurrentThresholdTrigger(TriggerDetector):
    requires_current = True

    def __init__(self, threshold_ua: float, *, direction: str = "above", hold_samples: int = 1):
        if direction not in ("above", "below"):
            raise UsageError(f"direction must be 'above' or 'below', got {direction!r}")
        if hold_samples < 1:
            raise UsageError("hold_samples must be >= 1")
        self.threshold_ua = threshold_ua
        self.direction = direction
        self.hold_samples = hold_samples
        self._hold = 0
        self._hold_start: int | None = None

    def feed(self, block: SampleBlock, currents_ua: list[float] | None) -> int | None:
        assert currents_ua is not None
        for offset, value in enumerate(currents_ua):
            match = (
                value > self.threshold_ua
                if self.direction == "above"
                else (value < self.threshold_ua)
            )
            if value != value:  # NaN: missing calibration never counts as evidence
                match = False
            if match:
                if self._hold == 0:
                    self._hold_start = block.start_index + offset
                self._hold += 1
                if self._hold >= self.hold_samples:
                    return self._hold_start
            else:
                self._hold = 0
                self._hold_start = None
        return None

    def on_gap(self, gap: GapEvent) -> None:
        self._hold = 0
        self._hold_start = None

    def describe(self) -> dict[str, Any]:
        return {
            "type": "current",
            "threshold_ua": self.threshold_ua,
            "direction": self.direction,
            "hold_samples": self.hold_samples,
        }


class DigitalPatternTrigger(TriggerDetector):
    def __init__(self, mask: int, value: int, *, hold_samples: int = 1):
        if not 0 <= mask <= 0xFF or not 0 <= value <= 0xFF:
            raise UsageError("mask and value must be 0-255")
        if value & ~mask:
            raise UsageError("value has bits outside the mask")
        self.mask = mask
        self.value = value
        self.hold_samples = hold_samples
        self._hold = 0
        self._hold_start: int | None = None

    def feed(self, block: SampleBlock, currents_ua: list[float] | None) -> int | None:
        for offset, byte in enumerate(block.logic):
            if (byte & self.mask) == self.value:
                if self._hold == 0:
                    self._hold_start = block.start_index + offset
                self._hold += 1
                if self._hold >= self.hold_samples:
                    return self._hold_start
            else:
                self._hold = 0
                self._hold_start = None
        return None

    def on_gap(self, gap: GapEvent) -> None:
        self._hold = 0
        self._hold_start = None

    def describe(self) -> dict[str, Any]:
        return {
            "type": "digital_pattern",
            "mask": self.mask,
            "value": self.value,
            "hold_samples": self.hold_samples,
        }


class DigitalEdgeTrigger(TriggerDetector):
    def __init__(self, channel: int, *, rising: bool = True):
        self.channel = channel
        self.rising = rising
        self._prev: int | None = None

    def feed(self, block: SampleBlock, currents_ua: list[float] | None) -> int | None:
        mask = 1 << self.channel
        prev = self._prev
        for offset, byte in enumerate(block.logic):
            level = 1 if byte & mask else 0
            if prev is not None and level != prev:
                fired = level == 1 if self.rising else level == 0
                if fired:
                    self._prev = level
                    return block.start_index + offset
            prev = level
        self._prev = prev
        return None

    def on_gap(self, gap: GapEvent) -> None:
        self._prev = None  # edges are never claimed across missing data

    def describe(self) -> dict[str, Any]:
        return {
            "type": "digital_edge",
            "channel": f"D{self.channel}",
            "edge": "rising" if self.rising else "falling",
        }


class UartContentTrigger(TriggerDetector):
    def __init__(self, decoder: UARTDecoder, pattern: bytes):
        if not pattern:
            raise UsageError("UART trigger pattern must not be empty")
        self.decoder = decoder
        self.pattern = pattern
        self._window: deque[tuple[int, int]] = deque(maxlen=len(pattern))

    def feed(self, block: SampleBlock, currents_ua: list[float] | None) -> int | None:
        annotations = self.decoder.feed(LogicChunk(block.start_index, block.logic))
        for ann in annotations:
            value = ann.fields.get("value") if ann.kind == "frame" and not ann.errors else None
            if value is None or value > 0xFF:
                # a gap, an unsynchronized or errored frame, a break, or a
                # value wider than a byte breaks the run: the pattern has to
                # be consecutive on the wire, not merely eventually present
                self._window.clear()
                continue
            self._window.append((value, ann.end_sample))
            if (
                len(self._window) == len(self.pattern)
                and bytes(v for v, _ in self._window) == self.pattern
            ):
                return self._window[-1][1]
        return None

    def on_gap(self, gap: GapEvent) -> None:
        self.decoder.notify_gap(gap)
        self._window.clear()

    def describe(self) -> dict[str, Any]:
        return {
            "type": "uart_content",
            "pattern": self.pattern.decode("ascii", errors="backslashreplace"),
            "uart": self.decoder.describe(),
        }


class SpiContentTrigger(TriggerDetector):
    def __init__(self, decoder: SPIDecoder, pattern: list[int], *, on: str = "mosi"):
        if not pattern:
            raise UsageError("SPI trigger pattern must not be empty")
        if on not in ("mosi", "miso"):
            raise UsageError(f"SPI trigger side must be 'mosi' or 'miso', got {on!r}")
        self.decoder = decoder
        self.pattern = list(pattern)
        self.on = on
        self._window: deque[tuple[int, int]] = deque(maxlen=len(pattern))

    def feed(self, block: SampleBlock, currents_ua: list[float] | None) -> int | None:
        annotations = self.decoder.feed(LogicChunk(block.start_index, block.logic))
        for ann in annotations:
            value = ann.fields.get(self.on) if ann.kind == "word" and not ann.errors else None
            if value is None:
                # a transaction boundary or an errored word breaks the run:
                # words from two exchanges were never one exchange
                self._window.clear()
                continue
            self._window.append((value, ann.end_sample))
            if (
                len(self._window) == len(self.pattern)
                and [v for v, _ in self._window] == self.pattern
            ):
                return self._window[-1][1]
        return None

    def on_gap(self, gap: GapEvent) -> None:
        self.decoder.notify_gap(gap)
        self._window.clear()

    def describe(self) -> dict[str, Any]:
        return {
            "type": "spi_content",
            "pattern": [f"0x{v:02x}" for v in self.pattern],
            "on": self.on,
            "spi": self.decoder.describe(),
        }


class TriggerEngine:
    """Ring-buffered pre/post trigger windowing over a live event stream."""

    def __init__(
        self,
        detector: TriggerDetector,
        *,
        pre_samples: int = 0,
        post_samples: int,
        calibration: Calibration | None = None,
        vdd_mv: int | None = None,
    ) -> None:
        if detector.requires_current and calibration is None:
            raise UsageError("this trigger needs calibrated current; no calibration available")
        if pre_samples < 0 or post_samples <= 0:
            raise UsageError("pre_samples must be >= 0 and post_samples > 0")
        self.detector = detector
        self.pre_samples = pre_samples
        self.post_samples = post_samples
        self.calibration = calibration
        self.vdd_mv = vdd_mv
        self.fired = False
        self.fire_index: int | None = None
        self.done = False
        self._ring: deque[SampleBlock | GapEvent] = deque()
        self._ring_samples = 0
        self._window_end: int | None = None

    def describe(self) -> dict[str, Any]:
        return {
            "detector": self.detector.describe(),
            "pre_samples": self.pre_samples,
            "post_samples": self.post_samples,
            "fired": self.fired,
            "fire_index": self.fire_index,
        }

    # -- internals ---------------------------------------------------------
    def _trim_event(
        self, event: SampleBlock | GapEvent, lo: int, hi: int
    ) -> SampleBlock | GapEvent | None:
        if isinstance(event, GapEvent):
            missing = event.missing
            g_lo = event.index
            g_hi = event.index + (missing or 0)
            if missing is None:
                return event if lo <= event.index < hi else None
            if g_hi <= lo or g_lo >= hi:
                return None
            new_lo, new_hi = max(g_lo, lo), min(g_hi, hi)
            return GapEvent(new_lo, new_hi - new_lo, event.reason, event.ambiguous)
        if event.end_index <= lo or event.start_index >= hi:
            return None
        s = max(0, lo - event.start_index)
        e = min(len(event), hi - event.start_index)
        if s == 0 and e == len(event):
            return event
        return SampleBlock(event.start_index + s, event.words[s:e])

    def process(self, event: SampleBlock | GapEvent) -> list[SampleBlock | GapEvent]:
        """Feed one live event; returns events belonging to the fired window."""
        if self.done:
            return []
        if not self.fired:
            fire: int | None = None
            if isinstance(event, GapEvent):
                self.detector.on_gap(event)
            else:
                currents = None
                if self.detector.requires_current and self.calibration is not None:
                    currents = self.calibration.convert_block(event, self.vdd_mv)
                fire = self.detector.feed(event, currents)
            if fire is None:
                # maintain the pre-trigger ring
                self._ring.append(event)
                if isinstance(event, SampleBlock):
                    self._ring_samples += len(event)
                while self._ring:
                    head = self._ring[0]
                    head_len = len(head) if isinstance(head, SampleBlock) else 0
                    if self._ring_samples - head_len >= self.pre_samples:
                        self._ring.popleft()
                        self._ring_samples -= head_len
                    else:
                        break
                return []
            self.fired = True
            self.fire_index = fire
            lo = fire - self.pre_samples
            hi = fire + self.post_samples
            self._window_end = hi
            out: list[SampleBlock | GapEvent] = []
            for buffered in self._ring:
                trimmed = self._trim_event(buffered, lo, hi)
                if trimmed is not None:
                    out.append(trimmed)
            self._ring.clear()
            self._ring_samples = 0
            trimmed = self._trim_event(event, lo, hi)
            if trimmed is not None:
                out.append(trimmed)
            self._check_done(event)
            return out
        assert self._window_end is not None
        lo = (self.fire_index or 0) - self.pre_samples
        trimmed = self._trim_event(event, lo, self._window_end)
        self._check_done(event)
        return [trimmed] if trimmed is not None else []

    def _check_done(self, event: SampleBlock | GapEvent) -> None:
        assert self._window_end is not None
        if isinstance(event, SampleBlock):
            if event.end_index >= self._window_end:
                self.done = True
        elif event.missing is not None and event.index + event.missing >= self._window_end:
            self.done = True


_CURRENT_SPEC_RE = re.compile(r"^current\s*([<>])\s*(\S+)$", re.IGNORECASE)
_DIGITAL_EDGE_RE = re.compile(r"^digital\s+(D[0-7])\s+(rising|falling)$", re.IGNORECASE)
_DIGITAL_PATTERN_RE = re.compile(
    r"^digital\s+mask=(0x[0-9a-f]+|\d+)\s+value=(0x[0-9a-f]+|\d+)$", re.IGNORECASE
)
_UART_SPEC_RE = re.compile(r'^uart\s+(D[0-7])\s+(\d+)\s+"([^"]+)"$', re.IGNORECASE)
_SPI_SPEC_RE = re.compile(
    r"^spi\s+((?:0x[0-9a-f]+|\d+)(?:\s*,\s*(?:0x[0-9a-f]+|\d+))*)$", re.IGNORECASE
)


def parse_trigger_spec(
    spec: str,
    *,
    hold_samples: int = 1,
    spi_config: dict[str, Any] | None = None,
    allow_experimental: bool = False,
) -> TriggerDetector:
    """Parse a CLI trigger spec into a detector.

    Forms::

        current>10mA          current<5uA
        digital D3 rising     digital D3 falling
        digital mask=0x0f value=0x05
        uart D0 9600 "BOOT"
        spi 0x9f,0x00         (channels from --spi-* options)
    """
    text = spec.strip()
    match = _CURRENT_SPEC_RE.match(text)
    if match:
        direction = "above" if match.group(1) == ">" else "below"
        return CurrentThresholdTrigger(
            parse_current_ua(match.group(2)), direction=direction, hold_samples=hold_samples
        )
    match = _DIGITAL_EDGE_RE.match(text)
    if match:
        return DigitalEdgeTrigger(
            parse_channel(match.group(1)), rising=match.group(2).lower() == "rising"
        )
    match = _DIGITAL_PATTERN_RE.match(text)
    if match:
        return DigitalPatternTrigger(
            int(match.group(1), 0), int(match.group(2), 0), hold_samples=hold_samples
        )
    match = _UART_SPEC_RE.match(text)
    if match:
        decoder = UARTDecoder(
            rx=match.group(1),
            baud=int(match.group(2)),
            allow_experimental=allow_experimental,
        )
        return UartContentTrigger(decoder, match.group(3).encode("ascii"))
    match = _SPI_SPEC_RE.match(text)
    if match:
        if not spi_config or spi_config.get("sclk") is None:
            raise UsageError(
                "SPI trigger needs channel configuration (--spi-sclk and --spi-mosi/miso)"
            )
        spi_decoder = SPIDecoder(
            sclk=spi_config["sclk"],
            mosi=spi_config.get("mosi"),
            miso=spi_config.get("miso"),
            cs=spi_config.get("cs"),
            mode=spi_config.get("mode", 0),
            word_bits=spi_config.get("word_bits", 8),
            expected_clock_hz=spi_config.get("expected_clock_hz"),
            allow_experimental=allow_experimental,
        )
        pattern = [int(v.strip(), 0) for v in match.group(1).split(",")]
        side = "mosi" if spi_config.get("mosi") is not None else "miso"
        return SpiContentTrigger(spi_decoder, pattern, on=side)
    raise UsageError(
        f"unrecognized trigger spec: {spec!r}",
        remediation='Use one of: current>10mA, current<5uA, "digital D3 rising", '
        '"digital mask=0x0f value=0x05", \'uart D0 9600 "BOOT"\', "spi 0x9f,0x00".',
    )
