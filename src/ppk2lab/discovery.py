"""PPK2 device discovery and measurement-port selection.

Since firmware 1.2.0 the device exposes a second (shell) CDC port, so the
first PPK2-looking serial port cannot be assumed to be the measurement port.
Ports are classified by USB interface number where the OS reports one;
otherwise the role stays ``unknown`` and callers may probe with the read-only
metadata command.
"""

from __future__ import annotations

import re

from .errors import TransportError
from .types import USB_PID, USB_VID, DeviceInfo, PortInfo, PortRole

#: USB locations end in ``<config>.<interface>``; the configuration field is
#: numeric on Linux/macOS but a letter on Windows (``1-4:x.0``), so it must
#: not be constrained to digits or Windows ports never classify at all.
_LOCATION_IFACE_RE = re.compile(r":(?:[^.:]+\.)?(\d+)$")


def _interface_number(entry: object) -> int | None:
    location = getattr(entry, "location", None)
    if location:
        match = _LOCATION_IFACE_RE.search(str(location))
        if match:
            return int(match.group(1))
    # Some platforms expose an explicit interface index attribute.
    interface = getattr(entry, "interface", None)
    if isinstance(interface, int):
        return interface
    return None


def _classify(ports: list[PortInfo]) -> list[PortInfo]:
    numbered = [p for p in ports if p.interface_number is not None]
    if not numbered:
        if len(ports) == 1:
            return [
                PortInfo(
                    path=ports[0].path,
                    role=PortRole.MEASUREMENT,
                    usb_location=ports[0].usb_location,
                    interface_number=None,
                )
            ]
        return ports  # ambiguous: leave every role unknown rather than guess
    lowest = min(p.interface_number for p in numbered)  # type: ignore[type-var]
    out = []
    for p in ports:
        if p.interface_number is None:
            role = PortRole.UNKNOWN
        elif p.interface_number == lowest:
            role = PortRole.MEASUREMENT
        else:
            role = PortRole.SHELL
        out.append(
            PortInfo(
                path=p.path,
                role=role,
                usb_location=p.usb_location,
                interface_number=p.interface_number,
            )
        )
    return out


def simulated_device_info(serial_number: str = "SIM0001") -> DeviceInfo:
    return DeviceInfo(
        serial_number=serial_number,
        vid=USB_VID,
        pid=USB_PID,
        ports=(
            PortInfo(
                path=f"simulated://{serial_number}",
                role=PortRole.MEASUREMENT,
                usb_location=None,
                interface_number=0,
            ),
        ),
        firmware_version="1.2.4-sim",
        simulated=True,
    )


def discover(*, simulate: bool = False) -> list[DeviceInfo]:
    """List connected PPK2 devices; deterministic ordering by serial number."""
    if simulate:
        return [simulated_device_info()]
    try:
        from serial.tools import list_ports
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise TransportError(
            "pyserial is not installed",
            remediation="Install ppk2lab with its dependencies: `pip install ppk2lab`.",
        ) from exc

    grouped: dict[str, list] = {}
    for entry in list_ports.comports():
        if entry.vid != USB_VID or entry.pid != USB_PID:
            continue
        key = entry.serial_number or entry.device
        grouped.setdefault(key, []).append(entry)

    devices: list[DeviceInfo] = []
    for key in sorted(grouped):
        entries = grouped[key]
        ports = [
            PortInfo(
                path=e.device,
                role=PortRole.UNKNOWN,
                usb_location=getattr(e, "location", None),
                interface_number=_interface_number(e),
            )
            for e in sorted(entries, key=lambda e: e.device)
        ]
        serial_number = entries[0].serial_number
        devices.append(
            DeviceInfo(
                serial_number=serial_number,
                vid=USB_VID,
                pid=USB_PID,
                ports=tuple(_classify(ports)),
                firmware_version=None,  # not observable without the shell protocol
                simulated=False,
            )
        )
    return devices
