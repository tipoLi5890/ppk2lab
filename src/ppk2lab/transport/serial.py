"""pyserial-backed transport for real PPK2 hardware."""

from __future__ import annotations

import errno

from ..errors import DeviceNotFoundError, PermissionDeniedError, PortBusyError, TransportError
from .base import Transport

#: The official app uses 115200; the physical throughput is USB CDC so the
#: line coding is not the real bottleneck. Overridable for behavior testing.
DEFAULT_BAUDRATE = 115200


class SerialTransport(Transport):
    def __init__(self, port: str, baudrate: int = DEFAULT_BAUDRATE) -> None:
        self.port = port
        self.baudrate = baudrate
        self._serial = None

    def open(self) -> None:
        if self._serial is not None:
            return
        try:
            import serial  # imported lazily so mock-only use never needs pyserial
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise TransportError(
                "pyserial is not installed",
                remediation="Install the package with `pip install ppk2lab` (pyserial is a "
                "required dependency) or `pip install pyserial`.",
            ) from exc
        try:
            self._serial = serial.Serial(
                self.port,
                baudrate=self.baudrate,
                timeout=0.1,
                write_timeout=2.0,
                exclusive=True,
            )
        except serial.SerialException as exc:
            raise _map_open_error(self.port, exc) from exc

    def close(self) -> None:
        if self._serial is not None:
            try:
                self._serial.close()
            finally:
                self._serial = None

    def write(self, data: bytes) -> None:
        if self._serial is None:
            raise TransportError(f"transport for {self.port} is not open")
        try:
            self._serial.write(data)
        except Exception as exc:
            raise TransportError(f"write to {self.port} failed: {exc}") from exc

    def read(self, max_bytes: int, timeout_s: float = 0.1) -> bytes:
        if self._serial is None:
            raise TransportError(f"transport for {self.port} is not open")
        try:
            self._serial.timeout = timeout_s
            first = self._serial.read(1)
            if not first:
                return b""
            pending = min(max_bytes - 1, self._serial.in_waiting)
            if pending > 0:
                return first + self._serial.read(pending)
            return first
        except Exception as exc:
            raise TransportError(f"read from {self.port} failed: {exc}") from exc

    @property
    def is_open(self) -> bool:
        return self._serial is not None

    @property
    def description(self) -> str:
        return self.port


def _map_open_error(port: str, exc: Exception) -> Exception:
    text = str(exc).lower()
    err_no = getattr(exc, "errno", None)
    if err_no == errno.EACCES or "permission" in text or "access is denied" in text:
        return PermissionDeniedError(f"permission denied opening {port}: {exc}")
    if err_no == errno.EBUSY or "busy" in text or "in use" in text or "exclusively" in text:
        return PortBusyError(f"port {port} is busy: {exc}")
    if err_no == errno.ENOENT or "could not open" in text or "no such" in text:
        return DeviceNotFoundError(f"port {port} does not exist: {exc}")
    return TransportError(f"failed to open {port}: {exc}")
