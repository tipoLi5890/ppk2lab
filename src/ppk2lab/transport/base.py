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
