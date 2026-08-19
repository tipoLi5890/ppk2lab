"""ppk2lab: control, capture, decode, and test Nordic PPK2 hardware from
Python, CLI automation, CI, and AI agents.

Quick start (simulated device, no hardware needed)::

    import ppk2lab

    with ppk2lab.PPK2.open(simulate=True) as dev:
        result = dev.capture(duration_s=0.5)
        print(result.stats.mean_ua)
"""

from ._version import SCHEMA_VERSION, __version__
from .aio import AsyncPPK2
from .calibration import Calibration
from .capture import Capture, CaptureResult, read_capture, write_capture
from .decoders import Annotation, SPIDecoder, UARTDecoder, decode_capture
from .device import PPK2
from .discovery import discover
from .errors import Ppk2labError
from .types import (
    DIGITAL_CHANNELS,
    SAMPLE_RATE_HZ,
    VOLTAGE_MAX_MV,
    VOLTAGE_MIN_MV,
    DeviceInfo,
    DeviceState,
    GapEvent,
    Mode,
    StateChange,
)

__all__ = [
    "DIGITAL_CHANNELS",
    "PPK2",
    "SAMPLE_RATE_HZ",
    "SCHEMA_VERSION",
    "VOLTAGE_MAX_MV",
    "VOLTAGE_MIN_MV",
    "Annotation",
    "AsyncPPK2",
    "Calibration",
    "Capture",
    "CaptureResult",
    "DeviceInfo",
    "DeviceState",
    "GapEvent",
    "Mode",
    "Ppk2labError",
    "SPIDecoder",
    "StateChange",
    "UARTDecoder",
    "__version__",
    "decode_capture",
    "discover",
    "read_capture",
    "write_capture",
]
