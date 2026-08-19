"""Transport layer: byte-level I/O to a PPK2 measurement port.

The device layer depends only on the :class:`Transport` interface, so tests
and agents can inject :class:`MockTransport` and never require hardware.
"""

from .base import Transport
from .mock import MockTransport, SimulatedPPK2
from .serial import SerialTransport

__all__ = ["MockTransport", "SerialTransport", "SimulatedPPK2", "Transport"]
