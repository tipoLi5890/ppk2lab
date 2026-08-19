"""Software triggers with pre/post-trigger capture windows."""

from .engine import (
    CurrentThresholdTrigger,
    DigitalEdgeTrigger,
    DigitalPatternTrigger,
    SpiContentTrigger,
    TriggerDetector,
    TriggerEngine,
    UartContentTrigger,
    parse_trigger_spec,
)

__all__ = [
    "CurrentThresholdTrigger",
    "DigitalEdgeTrigger",
    "DigitalPatternTrigger",
    "SpiContentTrigger",
    "TriggerDetector",
    "TriggerEngine",
    "UartContentTrigger",
    "parse_trigger_spec",
]
