"""Discovery and the real serial transport.

These paths decide which port a measurement runs on and what a user is told
when it cannot be opened, yet they only execute against hardware — so both
are exercised here against a fake ``serial`` module instead.
"""

from __future__ import annotations

import errno
import sys
import types

import pytest

from ppk2lab import discovery
from ppk2lab.errors import (
    DeviceNotFoundError,
    PermissionDeniedError,
    PortBusyError,
    TransportError,
)
from ppk2lab.transport.base import Transport
from ppk2lab.transport.serial import SerialTransport, _access_remediation, _map_open_error
from ppk2lab.types import USB_PID, USB_VID, PortRole


class _ComPort:
    """Mimics one pyserial ``ListPortInfo``."""

    def __init__(self, device, vid=USB_VID, pid=USB_PID, serial_number="ABC123", location=None):
        self.device = device
        self.vid = vid
        self.pid = pid
        self.serial_number = serial_number
        self.location = location


def _fake_list_ports(monkeypatch, entries):
    """Make pyserial enumerate exactly ``entries``, whatever is plugged in."""
    import serial.tools

    module = types.ModuleType("serial.tools.list_ports")
    module.comports = lambda: entries  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "serial.tools.list_ports", module)
    # `from serial.tools import list_ports` reads the attribute bound on the
    # package first and only falls back to sys.modules when there is none. One
    # real import anywhere in the process binds it for good, so patching
    # sys.modules alone lets a later test enumerate the developer's own PPK2.
    monkeypatch.setattr(serial.tools, "list_ports", module, raising=False)


# ---------------------------------------------------------------------------
# discover()


def test_discover_groups_ports_of_one_device(monkeypatch):
    _fake_list_ports(
        monkeypatch,
        [
            _ComPort("/dev/ttyACM1", location="1-1.1:1.2"),
            _ComPort("/dev/ttyACM0", location="1-1.1:1.0"),
        ],
    )
    devices = discovery.discover()
    assert len(devices) == 1
    device = devices[0]
    assert device.serial_number == "ABC123"
    # Ports are ordered deterministically, not by enumeration luck.
    assert [p.path for p in device.ports] == ["/dev/ttyACM0", "/dev/ttyACM1"]
    assert device.measurement_port is not None
    assert device.measurement_port.path == "/dev/ttyACM0"
    assert device.ports[1].role is PortRole.SHELL


def test_discover_ignores_other_usb_devices(monkeypatch):
    _fake_list_ports(
        monkeypatch,
        [
            _ComPort("/dev/ttyUSB0", vid=0x0403, pid=0x6001, serial_number="FTDI"),
            _ComPort("/dev/ttyACM0", pid=0x1234, serial_number="OTHER_NORDIC"),
        ],
    )
    assert discovery.discover() == []


def test_discover_orders_multiple_devices_deterministically(monkeypatch):
    _fake_list_ports(
        monkeypatch,
        [
            _ComPort("/dev/ttyACM2", serial_number="ZZZ"),
            _ComPort("/dev/ttyACM0", serial_number="AAA"),
        ],
    )
    assert [d.serial_number for d in discovery.discover()] == ["AAA", "ZZZ"]


def test_discover_keeps_a_device_without_a_serial_number(monkeypatch):
    """A missing serial number must not make the device invisible."""
    _fake_list_ports(monkeypatch, [_ComPort("/dev/ttyACM0", serial_number=None)])
    devices = discovery.discover()
    assert len(devices) == 1
    assert devices[0].serial_number is None
    assert devices[0].ports[0].role is PortRole.MEASUREMENT  # sole port


def test_discover_leaves_unclassifiable_ports_unknown(monkeypatch):
    """macOS reports no interface numbers; guessing would pick the shell port."""
    _fake_list_ports(
        monkeypatch,
        [_ComPort("/dev/cu.usbmodem1"), _ComPort("/dev/cu.usbmodem2")],
    )
    device = discovery.discover()[0]
    assert all(p.role is PortRole.UNKNOWN for p in device.ports)


def test_simulated_discovery_is_marked_as_such():
    device = discovery.discover(simulate=True)[0]
    assert device.simulated is True
    assert device.vid == USB_VID and device.pid == USB_PID


# ---------------------------------------------------------------------------
# SerialTransport error mapping


def test_permission_error_maps_to_platform_specific_remediation():
    class _SerialException(Exception):
        pass

    exc = _SerialException("could not open port: Permission denied")
    exc.errno = errno.EACCES
    mapped = _map_open_error("/dev/ttyACM0", exc)
    assert isinstance(mapped, PermissionDeniedError)
    assert mapped.remediation == _access_remediation()


def test_busy_port_maps_to_port_busy():
    exc = Exception("port is already in use")
    exc.errno = errno.EBUSY
    mapped = _map_open_error("COM3", exc)
    assert isinstance(mapped, PortBusyError)


def test_missing_port_maps_to_device_not_found():
    exc = Exception("could not open port /dev/ttyACM9: no such file or directory")
    exc.errno = errno.ENOENT
    assert isinstance(_map_open_error("/dev/ttyACM9", exc), DeviceNotFoundError)


def test_unrecognized_failure_stays_a_transport_error():
    mapped = _map_open_error("/dev/ttyACM0", Exception("something else entirely"))
    assert type(mapped) is TransportError


def test_access_remediation_names_the_platform_fix(monkeypatch):
    """The same errno has unrelated fixes per platform, so the text must differ."""
    monkeypatch.setattr(sys, "platform", "win32")
    assert "COM port" in _access_remediation()
    monkeypatch.setattr(sys, "platform", "linux")
    assert "dialout" in _access_remediation()


# ---------------------------------------------------------------------------
# SerialTransport lifecycle against a fake pyserial


class _FakeSerial:
    def __init__(self, *args, **kwargs):
        self.timeout = kwargs.get("timeout")
        self.closed = False
        self._data = bytearray(b"\x01\x02\x03\x04")
        self.timeout_assignments = 0
        self.flushed_before_close = False

    def __setattr__(self, name, value):
        if name == "timeout" and "timeout_assignments" in self.__dict__:
            self.__dict__["timeout_assignments"] += 1
        super().__setattr__(name, value)

    @property
    def in_waiting(self):
        return len(self._data)

    def read(self, n):
        chunk = bytes(self._data[:n])
        del self._data[:n]
        return chunk

    def write(self, data):
        return len(data)

    def flush(self):
        self.flushed_before_close = not self.closed

    def close(self):
        self.closed = True


def _install_fake_serial(monkeypatch, serial_cls=_FakeSerial):
    module = types.ModuleType("serial")
    module.Serial = serial_cls  # type: ignore[attr-defined]

    class SerialException(Exception):
        pass

    module.SerialException = SerialException  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "serial", module)
    return module


def test_serial_transport_open_read_close(monkeypatch):
    _install_fake_serial(monkeypatch)
    transport = SerialTransport("/dev/fake")
    transport.open()
    assert transport.is_open
    assert transport.description == "/dev/fake"
    assert transport.read(64, 0.1) == b"\x01\x02\x03\x04"
    transport.close()
    assert not transport.is_open


def test_serial_transport_does_not_reconfigure_the_port_per_read(monkeypatch):
    """Assigning pyserial's timeout can reconfigure the port; at 100 kS/s that
    is pure overhead on every read."""
    _install_fake_serial(monkeypatch)
    transport = SerialTransport("/dev/fake")
    transport.open()
    handle = transport._serial
    before = handle.timeout_assignments
    for _ in range(5):
        transport.read(4, 0.1)  # same timeout every time
    assert handle.timeout_assignments == before
    transport.read(4, 0.5)  # a different one must take effect
    assert handle.timeout_assignments == before + 1
    transport.close()


def test_serial_transport_bounds_a_hanging_open(monkeypatch):
    """Every other blocking path has a budget; open must not be the exception."""
    import threading

    class _HangingSerial:
        def __init__(self, *args, **kwargs):
            threading.Event().wait(30)

    _install_fake_serial(monkeypatch, _HangingSerial)
    transport = SerialTransport("/dev/wedged", open_timeout_s=0.2)
    with pytest.raises(TransportError) as excinfo:
        transport.open()
    assert "did not complete" in str(excinfo.value)
    assert "doctor" in excinfo.value.remediation


def test_reading_a_closed_transport_is_an_error(monkeypatch):
    _install_fake_serial(monkeypatch)
    transport = SerialTransport("/dev/fake")
    with pytest.raises(TransportError):
        transport.read(4)
    with pytest.raises(TransportError):
        transport.write(b"\x06")


# ---------------------------------------------------------------------------
# Multiple devices in one process


def test_two_simulated_devices_keep_separate_state():
    """Multi-device sessions must not share device state or command history."""
    from ppk2lab.device import PPK2
    from ppk2lab.transport.mock import MockTransport, SimulatedPPK2
    from ppk2lab.types import Mode

    sim_a = SimulatedPPK2(serial_number="SIM_A", initial_vdd_mv=3000)
    sim_b = SimulatedPPK2(serial_number="SIM_B", initial_vdd_mv=3300)
    a = PPK2.open(transport=MockTransport(sim_a), simulate=True)
    b = PPK2.open(transport=MockTransport(sim_b), simulate=True)
    try:
        assert a.state.source_voltage_mv == 3000
        assert b.state.source_voltage_mv == 3300

        a.set_source_voltage_mv(3600)
        assert sim_a.vdd_mv == 3600
        assert sim_b.vdd_mv == 3300  # untouched
        assert b.state.source_voltage_mv == 3300

        a.set_mode(Mode.AMPERE)
        assert sim_b.mode is Mode.SOURCE

        result_a = a.capture(duration_s=0.02)
        result_b = b.capture(duration_s=0.02)
        assert result_a.capture_id != result_b.capture_id
        assert len(a.session_log) != 0 and len(b.session_log) == 0
    finally:
        a.close()
        b.close()


def test_simulator_rejects_an_impossible_chunk_size_with_a_typed_error():
    """Every library error subclasses Ppk2labError, simulator setup included."""
    from ppk2lab.errors import Ppk2labError, UsageError
    from ppk2lab.transport.mock import SimulatedPPK2

    with pytest.raises(UsageError) as excinfo:
        SimulatedPPK2(metadata_chunk_bytes=0)
    assert isinstance(excinfo.value, Ppk2labError)
    assert excinfo.value.code == "INVALID_ARGUMENT"


def test_close_drains_the_write_buffer_first(monkeypatch):
    """A command byte still in the OS buffer has not reached the device.

    Closing a tty may discard queued output. Every command the PPK2 answers
    proves its own delivery through the reply, but DUT power has no readback
    at all, so a write lost at close would be reported as applied and never
    contradicted by anything.
    """
    _install_fake_serial(monkeypatch)
    transport = SerialTransport("/dev/fake")
    transport.open()
    handle = transport._serial
    transport.write(b"\x0d\x01")
    transport.close()
    assert handle.flushed_before_close is True


def test_flush_on_a_closed_transport_is_a_no_op(monkeypatch):
    """Cleanup runs on paths where the port is already gone."""
    _install_fake_serial(monkeypatch)
    transport = SerialTransport("/dev/fake")
    transport.flush()  # never opened
    transport.open()
    transport.close()
    transport.flush()  # already closed


def test_the_library_honours_the_simulate_environment_variable(monkeypatch):
    """A script and the equivalent command must behave the same under it."""
    from ppk2lab import PPK2, discover

    monkeypatch.setenv("PPK2LAB_SIMULATE", "1")
    assert [d.serial_number for d in discover()] == ["SIM0001"]
    with PPK2.open() as device:
        assert device.info.simulated is True


def test_an_explicit_choice_still_wins_over_the_environment(monkeypatch):
    from ppk2lab import discover

    monkeypatch.setenv("PPK2LAB_SIMULATE", "1")
    _fake_list_ports(monkeypatch, [_ComPort("/dev/ttyACM0", location="1-1.1:1.0")])
    devices = discover(simulate=False)
    assert [d.serial_number for d in devices] == ["ABC123"]
    assert all(not d.simulated for d in devices)


class _SilentTransport(Transport):
    """The minimum a caller can inject: a channel that answers nothing.

    Not ``MockTransport``, deliberately — a mock is a simulator by
    construction, and the question here is what identity a transport the
    library knows nothing about is given.
    """

    def __init__(self) -> None:
        self._open = False

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    def write(self, data: bytes) -> None:
        return None

    def read(self, max_bytes: int, timeout_s: float = 0.1) -> bytes:
        return b""

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def description(self) -> str:
        return "silent://injected"


def test_an_injected_transport_is_never_relabelled_by_the_environment(monkeypatch):
    """The environment must not decide the identity of a caller's transport.

    Under PPK2LAB_SIMULATE the injected channel used to be handed the
    simulator's identity — serial ``injected``, firmware ``1.2.4-sim``,
    ``simulated: true`` — while every byte still went out over the real
    channel. `run_capture` stores that identity in the manifest next to a
    firmware fingerprint read from the actual instrument, so a real
    measurement would be filed as a simulation.
    """
    from ppk2lab import PPK2

    monkeypatch.setenv("PPK2LAB_SIMULATE", "1")
    with PPK2.open(
        transport=_SilentTransport(), serial_number="PPK2-0042", read_metadata=False
    ) as device:
        assert device.info.simulated is False
        assert device.info.serial_number == "PPK2-0042"
        # No port was enumerated for it, and none may be invented: the
        # simulator's `simulated://...` path is what got fabricated before.
        assert device.info.ports == ()
        assert device.info.firmware_version is None


def test_an_injected_transport_can_still_be_declared_simulated(monkeypatch):
    """False-alarm guard: the fix must scope the label, not remove it."""
    from ppk2lab import PPK2
    from ppk2lab.transport.mock import MockTransport, SimulatedPPK2

    monkeypatch.delenv("PPK2LAB_SIMULATE", raising=False)
    with PPK2.open(transport=MockTransport(SimulatedPPK2()), simulate=True) as device:
        assert device.info.simulated is True
        assert device.info.serial_number == "injected"


@pytest.mark.parametrize("kwargs", [{}, {"serial_number": "SIM0001"}])
def test_an_explicit_simulate_false_is_not_answered_by_the_simulator(monkeypatch, kwargs):
    """``simulate=False`` has to escape the variable it exists to override.

    Device selection used to call a bare ``discover()``, which reads
    PPK2LAB_SIMULATE and enumerates the simulator — so ``open(simulate=False)``
    picked the simulated device and handed the literal string
    ``simulated://SIM0001`` to ``SerialTransport`` as a port path.
    """
    from ppk2lab import PPK2

    monkeypatch.setenv("PPK2LAB_SIMULATE", "1")
    _fake_list_ports(monkeypatch, [])
    with pytest.raises(DeviceNotFoundError) as excinfo:
        PPK2.open(simulate=False, **kwargs)
    # The exception type alone proves nothing: opening the simulator's port
    # path fails with ENOENT, which maps to DeviceNotFoundError too. What has
    # to hold is that nothing was enumerated to open in the first place.
    message = str(excinfo.value)
    assert "simulated://" not in message
    assert message.startswith("no PPK2")
