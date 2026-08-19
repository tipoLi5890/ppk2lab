"""Observable PPK2 host protocol: commands, metadata, and raw sample frames.

Behavior in this package is based on Nordic Semiconductor's official PPK2
documentation and the official Power Profiler app repository as compatibility
references; see docs/protocol-spec.md and docs/sources.md.
"""

from .commands import (
    Opcode,
    cmd_get_metadata,
    cmd_reset,
    cmd_set_dut_power,
    cmd_set_mode,
    cmd_set_source_voltage,
    cmd_set_user_gain,
    cmd_start_measuring,
    cmd_stop_measuring,
)
from .metadata import Metadata, parse_metadata
from .samples import (
    ADC_MULTIPLIER,
    RawSample,
    SampleBlock,
    SampleStreamParser,
    unpack_sample,
)

__all__ = [
    "ADC_MULTIPLIER",
    "Metadata",
    "Opcode",
    "RawSample",
    "SampleBlock",
    "SampleStreamParser",
    "cmd_get_metadata",
    "cmd_reset",
    "cmd_set_dut_power",
    "cmd_set_mode",
    "cmd_set_source_voltage",
    "cmd_set_user_gain",
    "cmd_start_measuring",
    "cmd_stop_measuring",
    "parse_metadata",
    "unpack_sample",
]
