"""pyserial-backed transport for real PPK2 hardware."""

from __future__ import annotations

import contextlib
import errno
import sys
import threading
from typing import Any

from ..errors import DeviceNotFoundError, PermissionDeniedError, PortBusyError, TransportError
from .base import Transport

#: The official app uses 115200; the physical throughput is USB CDC so the
#: line coding is not the real bottleneck. Overridable for behavior testing.
DEFAULT_BAUDRATE = 115200


class SerialTransport(Transport):
    def __init__(
        self,
        port: str,
        baudrate: int = DEFAULT_BAUDRATE,
        *,
        open_timeout_s: float = 10.0,
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self.open_timeout_s = open_timeout_s
        self._serial = None
        self._timeout: float | None = None

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
        # Opening is the only blocking step with no budget of its own: a
        # wedged USB stack can leave the constructor hanging indefinitely,
        # while every later read, write, and drain is bounded.
        result: list[Any] = []

        def _open() -> None:
            try:
                result.append(
                    serial.Serial(
                        self.port,
                        baudrate=self.baudrate,
                        timeout=0.1,
                        write_timeout=2.0,
                        exclusive=True,
                    )
                )
            except Exception as exc:
                result.append(exc)

        worker = threading.Thread(target=_open, name="ppk2lab-open", daemon=True)
        worker.start()
        worker.join(timeout=self.open_timeout_s)
        if not result:
            raise TransportError(
                f"opening {self.port} did not complete within {self.open_timeout_s:g} s",
                remediation="The serial stack is not responding. Replug the device, then "
                "run `ppk2lab doctor --json`.",
            )
        outcome = result[0]
        if isinstance(outcome, serial.SerialException):
            raise _map_open_error(self.port, outcome) from outcome
        if isinstance(outcome, Exception):
            raise TransportError(f"failed to open {self.port}: {outcome}") from outcome
        self._serial = outcome
        self._timeout = 0.1

    def flush(self) -> None:
        if self._serial is None:
            return
        # pyserial's flush is tcdrain: it blocks until the kernel has handed
        # every queued byte to the device.
        with contextlib.suppress(Exception):
            self._serial.flush()

    def close(self) -> None:
        if self._serial is not None:
            # Drain first. Closing a tty may discard queued output, and a
            # state-changing command lost that way is reported as applied
            # while the hardware never sees it — measured intermittently on
            # real hardware, where two of six DUT-power commands vanished.
            self.flush()
            try:
                self._serial.close()
            finally:
                self._serial = None
                self._timeout = None

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
            if self._timeout != timeout_s:
                # Assigning pyserial's timeout reconfigures the port on some
                # platforms; in a 100 kS/s hot path that is pure overhead.
                self._serial.timeout = timeout_s
                self._timeout = timeout_s
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


def _access_remediation() -> str:
    """Say what actually fixes access on *this* platform.

    On Windows an "access denied" almost always means another application
    holds the COM port, while on POSIX it usually means group membership —
    the same errno, two unrelated fixes.
    """
    if sys.platform.startswith("win"):
        return (
            "Another application is holding this COM port — usually the nRF Connect "
            "Power Profiler. Close it and retry; `ppk2lab discover --json` lists the ports."
        )
    if sys.platform == "darwin":
        return (
            "Close any other application using the port (the nRF Connect Power Profiler "
            "holds it exclusively), then retry."
        )
    return (
        "Add your user to the dialout/uucp group or install the udev rule for "
        "VID 1915 / PID c00a, then replug the device; see INSTALL.md."
    )


def _map_open_error(port: str, exc: Exception) -> Exception:
    text = str(exc).lower()
    err_no = getattr(exc, "errno", None)
    if err_no == errno.EACCES or "permission" in text or "access is denied" in text:
        return PermissionDeniedError(
            f"permission denied opening {port}: {exc}", remediation=_access_remediation()
        )
    if err_no == errno.EBUSY or "busy" in text or "in use" in text or "exclusively" in text:
        return PortBusyError(f"port {port} is busy: {exc}", remediation=_access_remediation())
    if err_no == errno.ENOENT or "could not open" in text or "no such" in text:
        return DeviceNotFoundError(f"port {port} does not exist: {exc}")
    return TransportError(f"failed to open {port}: {exc}")
