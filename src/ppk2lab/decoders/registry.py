"""Decoder registry: the live source for `ppk2lab capabilities`.

Capability output is generated from these definitions so the documented
surface cannot drift from the implementation.
"""

from __future__ import annotations

from typing import Any

from ..errors import UsageError
from ..types import SAMPLE_RATE_HZ
from .feasibility import CONDITIONAL_MIN, EXPERIMENTAL_MIN, VALIDATED_MIN
from .spi import SPIDecoder
from .uart import UARTDecoder

_DECODERS: dict[str, type] = {
    UARTDecoder.name: UARTDecoder,
    SPIDecoder.name: SPIDecoder,
}


def get_decoder_class(name: str) -> type:
    try:
        return _DECODERS[name]
    except KeyError as exc:
        raise UsageError(
            f"unknown decoder {name!r}; available: {', '.join(sorted(_DECODERS))}"
        ) from exc


def decoder_capabilities() -> list[dict[str, Any]]:
    rate = SAMPLE_RATE_HZ
    return [
        {
            "name": "uart",
            "channels": {"rx": "required (D0-D7)"},
            "options": {
                "baud": "int, required",
                "data_bits": "5-9 (default 8)",
                "parity": "none|even|odd (default none)",
                "stop_bits": "1|2 (default 1)",
                "msb_first": "bool (default false)",
                "invert": "bool (default false)",
            },
            "rate_tiers": {
                "validated_max_baud": int(rate / VALIDATED_MIN),  # 10000 -> 9600 in practice
                "conditional_max_baud": int(rate / CONDITIONAL_MIN),
                "experimental_max_baud": int(rate / EXPERIMENTAL_MIN),
                "note": "UART <= 9600 baud is the validated 0.1.0 target; 19200 is "
                "conditional; 38400 is experimental; above is refused.",
            },
            "gap_policy": "frames touching a sample gap are reported as errors with "
            "confidence 0; no decoding is claimed across missing data",
        },
        {
            "name": "spi",
            "channels": {
                "sclk": "required (D0-D7)",
                "mosi": "optional",
                "miso": "optional (at least one of mosi/miso)",
                "cs": "optional",
            },
            "options": {
                "mode": "0-3 (CPOL/CPHA)",
                "word_bits": "4-32 (default 8)",
                "msb_first": "bool (default true)",
                "cs_active_low": "bool (default true)",
                "expected_clock_hz": "float, optional; enables rate-tier validation",
                "idle_timeout_samples": "int, optional; transaction grouping without CS",
            },
            "rate_tiers": {
                "validated_max_hz": int(rate / VALIDATED_MIN),
                "conditional_max_hz": int(rate / CONDITIONAL_MIN),
                "experimental_max_hz": int(rate / EXPERIMENTAL_MIN),
                "note": "SPI <= 10 kHz SCLK is the validated 0.1.0 target; <= 20 kHz is "
                "conditional; <= 40 kHz is experimental; above is refused.",
            },
            "gap_policy": "words/transactions touching a sample gap are closed with a "
            "gap error and confidence 0",
        },
    ]
