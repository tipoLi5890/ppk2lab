"""Streaming protocol decoders operating on the synchronized D0-D7 stream."""

from .base import Annotation, LogicChunk, StreamingDecoder, decode_capture
from .feasibility import Feasibility, Tier, spi_feasibility, uart_feasibility
from .registry import decoder_capabilities, get_decoder_class
from .spi import SPIDecoder
from .uart import UARTDecoder

__all__ = [
    "Annotation",
    "Feasibility",
    "LogicChunk",
    "SPIDecoder",
    "StreamingDecoder",
    "Tier",
    "UARTDecoder",
    "decode_capture",
    "decoder_capabilities",
    "get_decoder_class",
    "spi_feasibility",
    "uart_feasibility",
]
