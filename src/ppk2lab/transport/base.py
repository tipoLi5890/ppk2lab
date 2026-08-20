"""Abstract transport interface."""

from __future__ import annotations

import abc


class Transport(abc.ABC):
    """Byte-level duplex channel to a PPK2 measurement port."""

    @abc.abstractmethod
    def open(self) -> None:
        """Open the channel. Raises a ppk2lab error subclass on failure."""

    @abc.abstractmethod
    def close(self) -> None:
        """Close the channel; idempotent."""

    def flush(self) -> None:
        """Block until everything written has actually left the host.

        A command byte sitting in the OS write buffer has not reached the
        device, and closing the port can discard it. That is invisible for
        every command the PPK2 answers — the reply proves delivery — but DUT
        power has no readback at all, so nothing else would ever surface a
        lost write. Default is a no-op for transports that cannot buffer.
        """
        return None

    @abc.abstractmethod
    def write(self, data: bytes) -> None:
        """Write all of ``data`` to the device."""

    @abc.abstractmethod
    def read(self, max_bytes: int, timeout_s: float = 0.1) -> bytes:
        """Read up to ``max_bytes``; returns ``b""`` on timeout."""

    @property
    @abc.abstractmethod
    def is_open(self) -> bool: ...

    @property
    @abc.abstractmethod
    def description(self) -> str:
        """Human-readable identity (port path or simulator tag)."""

    def __enter__(self) -> Transport:
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
