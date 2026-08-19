"""Shared public types for devices, state changes, and capture streams."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any

#: Fixed PPK2 sample rate. Current and D0-D7 share one synchronized timeline.
SAMPLE_RATE_HZ = 100_000
#: One sample every 10 us.
SAMPLE_PERIOD_NS = 10_000
SAMPLE_PERIOD_S = SAMPLE_PERIOD_NS / 1e9

#: Nordic Semiconductor USB vendor ID and the PPK2 product ID.
USB_VID = 0x1915
USB_PID = 0xC00A

#: Source voltage limits in millivolts (device capability).
VOLTAGE_MIN_MV = 800
VOLTAGE_MAX_MV = 5000

#: Digital channel names in bit order: bit N of the logic byte is D<N>.
DIGITAL_CHANNELS = tuple(f"D{i}" for i in range(8))


class Mode(enum.IntEnum):
    """PPK2 measurement mode. Wire values match the 0x11 command payload."""

    AMPERE = 1
    SOURCE = 2


class PortRole(enum.Enum):
    """Role of a USB CDC serial interface exposed by the PPK2."""

    MEASUREMENT = "measurement"
    SHELL = "shell"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PortInfo:
    """One serial port belonging to a PPK2 device."""

    path: str
    role: PortRole = PortRole.UNKNOWN
    usb_location: str | None = None
    interface_number: int | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "role": self.role.value,
            "usb_location": self.usb_location,
            "interface_number": self.interface_number,
        }


@dataclass(frozen=True)
class DeviceInfo:
    """Discovery result for one PPK2 device (may expose multiple ports)."""

    serial_number: str | None
    vid: int | None
    pid: int | None
    ports: tuple[PortInfo, ...]
    firmware_version: str | None = None
    simulated: bool = False

    @property
    def measurement_port(self) -> PortInfo | None:
        for port in self.ports:
            if port.role is PortRole.MEASUREMENT:
                return port
        return self.ports[0] if self.ports else None

    def to_json(self) -> dict[str, Any]:
        return {
            "serial_number": self.serial_number,
            "vid": self.vid,
            "pid": self.pid,
            "ports": [p.to_json() for p in self.ports],
            "firmware_version": self.firmware_version,
            "simulated": self.simulated,
        }


class VoltageBasis(enum.Enum):
    """Where a voltage value came from.

    The PPK2 never measures the DUT's terminal voltage, so any energy figure
    is derived from an assumption. This enum records which assumption, so
    results can say how much the number is worth.
    """

    #: The caller supplied the voltage explicitly (e.g. --assume-voltage-mv).
    CALLER_OVERRIDE = "caller_override"
    #: This session set the source voltage; in Source Meter mode the DUT is
    #: powered from VOUT, so the setpoint is a defensible supply voltage.
    CONFIGURED_SOURCE = "configured_source"
    #: Read from device metadata (the regulator setpoint). In Ampere Meter
    #: mode this is a leftover setpoint unrelated to the DUT's own supply.
    DEVICE_METADATA = "device_metadata"
    #: No voltage is known.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class DeviceState:
    """Device state as tracked by the host session.

    ``None`` means "unknown / not observable"; unknown values are preserved,
    never replaced with invented defaults.

    Frozen, and reached through the read-only :attr:`ppk2lab.PPK2.state`
    property, because ``dut_power`` can never be read back from the device:
    a host-side write to it would be recorded in a capture manifest as a
    hardware state, indistinguishable from one the tool actually commanded.
    Only the device driver updates this, and only after the command went out.
    """

    mode: Mode | None = None
    source_voltage_mv: int | None = None
    dut_power: bool | None = None
    measuring: bool = False
    #: How ``source_voltage_mv`` was learned; see :class:`VoltageBasis`.
    source_voltage_basis: VoltageBasis = VoltageBasis.UNKNOWN

    def to_json(self) -> dict[str, Any]:
        return {
            "mode": self.mode.name.lower() if self.mode is not None else None,
            "source_voltage_mv": self.source_voltage_mv,
            "dut_power": self.dut_power,
            "measuring": self.measuring,
            "source_voltage_basis": self.source_voltage_basis.value,
        }


@dataclass
class StateChange:
    """Machine-readable record of a state-changing operation.

    ``observed`` distinguishes values read back from the device from values
    the host merely requested; agents must not treat requested state as
    confirmed hardware state.
    """

    operation: str
    requested: dict[str, Any]
    before: dict[str, Any]
    after: dict[str, Any]
    applied: bool
    observed_after: bool = False
    warnings: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "requested": self.requested,
            "before": self.before,
            "after": self.after,
            "applied": self.applied,
            "observed_after": self.observed_after,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class GapEvent:
    """A detected loss of samples in the 100 kS/s stream.

    ``index`` is the timeline sample index at which the missing run begins.
    ``missing`` is the number of missing samples, or ``None`` when the true
    count is unknown (for example a host-side queue overflow). ``ambiguous``
    is True when the count was derived from the 6-bit counter and could be
    larger by a multiple of 64.
    """

    index: int
    missing: int | None
    reason: str = "counter_skip"
    ambiguous: bool = True

    def to_json(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "missing": self.missing,
            "reason": self.reason,
            "ambiguous": self.ambiguous,
        }
