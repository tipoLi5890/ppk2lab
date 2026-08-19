"""Synchronous PPK2 device API.

Every state-changing operation returns a :class:`~ppk2lab.types.StateChange`
with requested/before/after state and whether the "after" state was actually
read back from the device. Nothing here ever enables DUT power implicitly.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterator
from typing import Any

from .calibration import Calibration
from .discovery import discover, simulated_device_info
from .errors import (
    DeviceNotFoundError,
    MetadataError,
    UsageError,
    VoltageRangeError,
)
from .protocol.commands import (
    cmd_get_metadata,
    cmd_reset,
    cmd_set_dut_power,
    cmd_set_mode,
    cmd_set_source_voltage,
    cmd_set_user_gain,
    cmd_start_measuring,
    cmd_stop_measuring,
)
from .protocol.metadata import Metadata, parse_metadata
from .protocol.samples import GapEvent, SampleBlock
from .session import StreamSession
from .transport.base import Transport
from .transport.mock import MockTransport, SimulatedPPK2
from .transport.serial import SerialTransport
from .types import DeviceInfo, DeviceState, Mode, PortRole, StateChange, VoltageBasis

#: Factory for real serial transports; tests monkeypatch this to exercise
#: the measurement-port probing path without hardware.
_transport_factory = SerialTransport

#: Environment variable holding a session-wide source-voltage ceiling in mV.
MAX_VOLTAGE_ENV = "PPK2LAB_MAX_VOLTAGE_MV"


def _drain_input(transport: Transport, *, max_seconds: float = 3.0, quiet_reads: int = 3) -> int:
    """Read and discard stale input until the channel stays quiet."""
    drained = 0
    quiet = 0
    deadline = time.monotonic() + max_seconds
    while time.monotonic() < deadline and quiet < quiet_reads:
        chunk = transport.read(65536, 0.05)
        if chunk:
            drained += len(chunk)
            quiet = 0
        else:
            quiet += 1
    return drained


def _metadata_terminated(buf: bytes | bytearray) -> bool:
    """True once the metadata reply's ``END`` terminator sits at a line start.

    Anchoring to a line boundary stops stray bytes that happen to spell
    ``END`` (binary residue from an interrupted stream) from truncating an
    otherwise complete reply.
    """
    return buf.startswith(b"END") or b"\nEND" in buf or b"\rEND" in buf


def _env_voltage_ceiling() -> int | None:
    raw = os.environ.get(MAX_VOLTAGE_ENV)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise UsageError(
            f"{MAX_VOLTAGE_ENV} must be an integer number of millivolts, got {raw!r}"
        ) from exc


class PPK2:
    """One open PPK2 measurement session."""

    def __init__(
        self, transport: Transport, info: DeviceInfo, *, max_voltage_mv: int | None = None
    ) -> None:
        self.transport = transport
        self.info = info
        self.state = DeviceState()
        self.metadata: Metadata | None = None
        self.calibration: Calibration | None = None
        self.session_log: list[StateChange] = []
        #: Stale stream bytes discarded at open (leftover from an interrupted
        #: previous session); nonzero values are surfaced as a warning.
        self.recovered_stale_bytes = 0
        #: Optional session ceiling on the source voltage, below the device
        #: limit — a guard for DUTs that would be damaged above a known level.
        self.max_voltage_mv = (
            max_voltage_mv if max_voltage_mv is not None else _env_voltage_ceiling()
        )
        self._voltage_configured = False
        self._initial_dut_power: bool | None = None
        self._power_changed = False
        self._closed = False
        #: One device owns one serial channel: interleaved reads from another
        #: thread would silently corrupt 4-byte framing.
        self._stream_active = False
        self._lock = threading.RLock()

    # -- guards ------------------------------------------------------------
    def _require_no_active_stream(self, action: str) -> None:
        if self._stream_active:
            raise UsageError(
                f"cannot {action} while a capture stream is active on this device",
                remediation="Let the active stream finish (or close its iterator) before "
                "issuing other commands. One device serves one stream at a time.",
            )

    def _require_not_measuring(self, action: str) -> None:
        if self.state.measuring:
            raise UsageError(
                f"cannot {action} while the device is measuring",
                remediation="Call stop_measuring() first; changing acquisition settings "
                "mid-stream would silently split the capture into incomparable halves.",
            )

    def _check_voltage_ceiling(self, voltage_mv: int) -> None:
        ceiling = self.max_voltage_mv
        if ceiling is not None and isinstance(voltage_mv, int) and voltage_mv > ceiling:
            raise VoltageRangeError(
                f"voltage_mv={voltage_mv} exceeds the configured session ceiling of {ceiling} mV",
                remediation="This ceiling protects the DUT. Raise it deliberately with "
                f"--max-voltage-mv / {MAX_VOLTAGE_ENV} only if the DUT tolerates more.",
            )

    # -- lifecycle ---------------------------------------------------------
    @classmethod
    def open(
        cls,
        *,
        serial_number: str | None = None,
        port: str | None = None,
        transport: Transport | None = None,
        simulate: bool = False,
        simulator: SimulatedPPK2 | None = None,
        read_metadata: bool = True,
        max_voltage_mv: int | None = None,
    ) -> PPK2:
        """Open a device by serial number, port path, or injected transport.

        Opening always performs interrupted-session recovery first: a stop
        command plus an input drain, so a device left streaming by a previous
        session cannot corrupt the metadata read. When the OS does not expose
        USB interface numbers (observed on macOS), the measurement port is
        identified by probing candidates with the read-only metadata command.
        """
        if transport is not None:
            info = (
                simulated_device_info("injected")
                if simulate
                else DeviceInfo(
                    serial_number=serial_number, vid=None, pid=None, ports=(), simulated=False
                )
            )
        elif simulate or simulator is not None:
            if simulator is None:
                from .testing.profiles import DemoActivityProfile

                simulator = SimulatedPPK2(profile=DemoActivityProfile())
            info = simulated_device_info(simulator.serial_number)
            transport = MockTransport(simulator)
        else:
            info = _select_device(serial_number=serial_number, port=port)
            return cls._open_real(
                info, port=port, read_metadata=read_metadata, max_voltage_mv=max_voltage_mv
            )

        transport.open()
        device = cls(transport, info, max_voltage_mv=max_voltage_mv)
        try:
            device.recover_session()
            if read_metadata:
                device.refresh_metadata()
        except Exception:
            transport.close()
            raise
        return device

    @classmethod
    def _open_real(
        cls,
        info: DeviceInfo,
        *,
        port: str | None,
        read_metadata: bool,
        max_voltage_mv: int | None = None,
    ) -> PPK2:
        candidates: list[str]
        probing = False
        if port is not None:
            candidates = [port]
        else:
            measurement = info.measurement_port
            if measurement is None:
                raise DeviceNotFoundError(f"device {info.serial_number} exposes no serial ports")
            if measurement.role is PortRole.UNKNOWN and len(info.ports) > 1:
                # Roles could not be classified from USB metadata: identify
                # the measurement port by a read-only metadata probe.
                candidates = [p.path for p in info.ports]
                probing = True
            else:
                candidates = [measurement.path]

        failures: list[str] = []
        for path in candidates:
            transport = _transport_factory(path)
            device = cls(transport, info, max_voltage_mv=max_voltage_mv)
            try:
                transport.open()
                device.recover_session()
                if read_metadata or probing:
                    device.refresh_metadata()
                return device
            except Exception as exc:
                transport.close()
                if not probing:
                    raise
                failures.append(f"{path}: {exc}")
        raise DeviceNotFoundError(
            f"no port of {info.serial_number} answered the metadata probe ({'; '.join(failures)})",
            remediation="Check the USB connection and close other apps using the "
            "device, then retry; or pass the measurement port explicitly with --port.",
        )

    def recover_session(self) -> int:
        """Stop any leftover measurement and drain stale stream data.

        Returns the number of stale bytes discarded. Sends only the stop
        command — DUT power, voltage, and mode are untouched.
        """
        self.transport.write(cmd_stop_measuring())
        self.recovered_stale_bytes = _drain_input(self.transport)
        self.state.measuring = False
        return self.recovered_stale_bytes

    def close(self, *, restore_power: bool = True) -> list[StateChange]:
        """Close the session, restoring the session-start DUT power state.

        Restoration is best-effort: failures are recorded in the returned
        state changes rather than raised, and the transport always closes.
        """
        if self._closed:
            return []
        changes: list[StateChange] = []
        try:
            if self.state.measuring:
                try:
                    self.stop_measuring()
                except Exception as exc:
                    changes.append(
                        StateChange(
                            operation="stop_measuring",
                            requested={"measuring": False},
                            before=self.state.to_json(),
                            after=self.state.to_json(),
                            applied=False,
                            warnings=[f"stop failed during close: {exc}"],
                        )
                    )
            if restore_power and self._power_changed:
                target = self._initial_dut_power
                warnings = []
                if target is None:
                    target = False
                    warnings.append(
                        "initial DUT power state was unknown; power set OFF as fail-safe"
                    )
                try:
                    change = self.set_dut_power(target)
                    change.warnings.extend(warnings)
                    change.warnings.append("restored session-start power state on close")
                    changes.append(change)
                except Exception as exc:
                    changes.append(
                        StateChange(
                            operation="set_dut_power",
                            requested={"dut_power": target},
                            before=self.state.to_json(),
                            after=self.state.to_json(),
                            applied=False,
                            warnings=[*warnings, f"power restoration failed: {exc}"],
                        )
                    )
        finally:
            self.transport.close()
            self._closed = True
        self.session_log.extend(changes)
        return changes

    def __enter__(self) -> PPK2:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- metadata ----------------------------------------------------------
    def refresh_metadata(self, timeout_s: float = 2.0) -> Metadata:
        """Read and parse device metadata (read-only, opcode 0x19).

        The reply arrives as newline-delimited text that may be split across
        several serial reads; bytes are accumulated until the terminator is
        seen at a line boundary, so a partial read can never drop calibration
        constants (and a stray ``END`` inside binary residue cannot end the
        read early).
        """
        self._require_no_active_stream("read metadata")
        if self.state.measuring:
            raise UsageError("metadata cannot be read while measuring; stop first")
        self.transport.write(cmd_get_metadata())
        buf = bytearray()
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            chunk = self.transport.read(4096, 0.1)
            if chunk:
                buf.extend(chunk)
                if _metadata_terminated(buf):
                    break
        if not buf:
            raise MetadataError("device sent no metadata reply")
        metadata = parse_metadata(buf.decode("ascii", errors="replace"))
        self.metadata = metadata
        self.calibration = Calibration.from_metadata(metadata)
        if metadata.mode in (int(Mode.AMPERE), int(Mode.SOURCE)):
            self.state.mode = Mode(metadata.mode)
        if metadata.vdd_mv is not None:
            self.state.source_voltage_mv = metadata.vdd_mv
            # A readback after this session set the voltage keeps the stronger
            # basis; otherwise the value is just the regulator's setpoint.
            if not self._voltage_configured:
                self.state.source_voltage_basis = VoltageBasis.DEVICE_METADATA
        return metadata

    def firmware_fingerprint(self) -> dict[str, Any]:
        """Identify the firmware by what is actually observable.

        The PPK2 does not report a firmware version over the measurement
        port, so compatibility has to be keyed on observable structure: the
        hardware revision, the instrumentation-amplifier field, which
        metadata keys exist, and how many serial ports the device exposes.
        Two units that agree on all four behave the same for our purposes.
        """
        metadata = self.metadata
        keys: list[str] = []
        if metadata is not None:
            families = ("R", "GS", "GI", "O", "S", "I", "UG")
            present = [
                f"{family}{i}"
                for family in families
                for i in range(5)
                if metadata.cal[family][i] is not None
            ]
            scalars = [
                name
                for name, value in (
                    ("Calibrated", metadata.calibrated),
                    ("VDD", metadata.vdd_mv),
                    ("HW", metadata.hw),
                    ("mode", metadata.mode),
                    ("IA", metadata.ia),
                )
                if value is not None
            ]
            keys = sorted(present + scalars + list(metadata.extras))
        return {
            "hw": getattr(metadata, "hw", None),
            "ia": getattr(metadata, "ia", None),
            "metadata_keys": keys,
            "metadata_key_count": len(keys),
            "port_count": len(self.info.ports),
        }

    # -- state-changing operations ----------------------------------------
    def _readback(self, warnings: list[str]) -> bool:
        try:
            self.refresh_metadata()
            return True
        except Exception as exc:
            warnings.append(f"state readback failed: {exc}")
            return False

    def set_mode(self, mode: Mode, *, dry_run: bool = False) -> StateChange:
        request = cmd_set_mode(mode)  # validates before touching hardware
        before = self.state.to_json()
        warnings: list[str] = []
        if mode is Mode.AMPERE:
            warnings.append(
                "ampere mode measures current flowing through the meter; the DUT must be "
                "powered from its own supply (VOUT does not power it). The source voltage "
                "still matters: it feeds the calibration correction term."
            )
        if dry_run:
            projected = dict(before, mode=mode.name.lower())
            change = StateChange(
                "set_mode",
                {"mode": mode.name.lower()},
                before,
                projected,
                applied=False,
                warnings=warnings,
            )
        else:
            self._require_no_active_stream("change mode")
            self._require_not_measuring("change mode")
            self.transport.write(request)
            self.state.mode = mode
            observed = self._readback(warnings)
            if observed and self.state.mode is not mode:
                warnings.append(f"device reports mode {self.state.mode} after requesting {mode}")
            change = StateChange(
                "set_mode",
                {"mode": mode.name.lower()},
                before,
                self.state.to_json(),
                applied=True,
                observed_after=observed,
                warnings=warnings,
            )
        self.session_log.append(change)
        return change

    def set_source_voltage_mv(self, voltage_mv: int, *, dry_run: bool = False) -> StateChange:
        self._check_voltage_ceiling(voltage_mv)
        request = cmd_set_source_voltage(voltage_mv)  # validates range first
        before = self.state.to_json()
        warnings: list[str] = []
        if dry_run:
            projected = dict(before, source_voltage_mv=voltage_mv)
            change = StateChange(
                "set_source_voltage_mv",
                {"voltage_mv": voltage_mv},
                before,
                projected,
                applied=False,
            )
        else:
            self._require_no_active_stream("change the source voltage")
            self._require_not_measuring("change the source voltage")
            self.transport.write(request)
            self.state.source_voltage_mv = voltage_mv
            self.state.source_voltage_basis = VoltageBasis.CONFIGURED_SOURCE
            self._voltage_configured = True
            observed = self._readback(warnings)
            if observed and self.state.source_voltage_mv != voltage_mv:
                warnings.append(
                    f"device reports VDD {self.state.source_voltage_mv} mV after "
                    f"requesting {voltage_mv} mV"
                )
            change = StateChange(
                "set_source_voltage_mv",
                {"voltage_mv": voltage_mv},
                before,
                self.state.to_json(),
                applied=True,
                observed_after=observed,
                warnings=warnings,
            )
        self.session_log.append(change)
        return change

    def set_dut_power(self, on: bool, *, dry_run: bool = False) -> StateChange:
        request = cmd_set_dut_power(on)
        before = self.state.to_json()
        if dry_run:
            projected = dict(before, dut_power=on)
            change = StateChange(
                "set_dut_power", {"dut_power": on}, before, projected, applied=False
            )
        else:
            if not self._power_changed:
                self._initial_dut_power = self.state.dut_power
            self.transport.write(request)
            self.state.dut_power = on
            self._power_changed = True
            change = StateChange(
                "set_dut_power",
                {"dut_power": on},
                before,
                self.state.to_json(),
                applied=True,
                observed_after=False,
                warnings=[
                    "DUT power state cannot be read back from device metadata; "
                    "'after' reflects the requested state"
                ],
            )
        self.session_log.append(change)
        return change

    def reset(self, *, dry_run: bool = False) -> StateChange:
        """Reset the device (experimental).

        The observable consequences of opcode 0x20 — whether the USB CDC port
        re-enumerates, whether DUT power drops momentarily, how long the port
        stays away — are not yet confirmed across firmware versions
        (docs/protocol-spec.md, "Unverified items"). Treat a reset as a
        session boundary: reopen the device afterwards rather than assuming
        the handle survives.
        """
        before = self.state.to_json()
        warnings: list[str] = [
            "reset is experimental: port re-enumeration and DUT power behavior are not "
            "hardware-verified across firmware versions; reopen the device afterwards"
        ]
        if dry_run:
            change = StateChange("reset", {}, before, before, applied=False, warnings=warnings)
        else:
            self._require_no_active_stream("reset the device")
            self._require_not_measuring("reset the device")
            self.transport.write(cmd_reset())
            self.state = DeviceState()  # all state unknown after reset
            self._voltage_configured = False
            observed = self._readback(warnings)
            change = StateChange(
                "reset",
                {},
                before,
                self.state.to_json(),
                applied=True,
                observed_after=observed,
                warnings=warnings,
            )
        self.session_log.append(change)
        return change

    def set_user_gain(self, range_index: int, gain: float, *, dry_run: bool = False) -> StateChange:
        request = cmd_set_user_gain(range_index, gain)
        before = self.state.to_json()
        if dry_run:
            change = StateChange(
                "set_user_gain",
                {"range_index": range_index, "gain": gain},
                before,
                before,
                applied=False,
            )
        else:
            self._require_no_active_stream("set a user gain")
            self._require_not_measuring("set a user gain")
            self.transport.write(request)
            warnings: list[str] = []
            observed = self._readback(warnings)
            change = StateChange(
                "set_user_gain",
                {"range_index": range_index, "gain": gain},
                before,
                self.state.to_json(),
                applied=True,
                observed_after=observed,
                warnings=warnings,
            )
        self.session_log.append(change)
        return change

    # -- measurement -------------------------------------------------------
    def start_measuring(self) -> None:
        self.transport.write(cmd_start_measuring())
        self.state.measuring = True

    def stop_measuring(self) -> None:
        self.transport.write(cmd_stop_measuring())
        self.state.measuring = False

    def stream(
        self,
        *,
        sample_limit: int | None = None,
        duration_s: float | None = None,
        wall_timeout_s: float | None = None,
    ) -> Iterator[SampleBlock | GapEvent]:
        """Start measuring and yield loss-aware stream events.

        Stops measuring when the iterator is closed or exhausted.
        """
        from .types import SAMPLE_RATE_HZ

        if duration_s is not None:
            limit = round(duration_s * SAMPLE_RATE_HZ)
            sample_limit = min(sample_limit, limit) if sample_limit else limit
        if wall_timeout_s is None:
            # Always arm a wall budget: a device that stops streaming while
            # keeping the port open would otherwise block forever.
            if duration_s is not None:
                wall_timeout_s = duration_s * 3 + 10.0
            elif sample_limit is not None:
                wall_timeout_s = (sample_limit / SAMPLE_RATE_HZ) * 3 + 10.0
        self._require_no_active_stream("start another stream")
        session = StreamSession(self.transport)
        self._stream_active = True
        self.start_measuring()
        session.start()
        try:
            yield from session.events(sample_limit=sample_limit, wall_timeout_s=wall_timeout_s)
        finally:
            self._stream_active = False
            session.stop()
            try:
                self.stop_measuring()
            except Exception:
                self.state.measuring = False

    def capture(self, **kwargs: Any):
        """Capture to a :class:`~ppk2lab.capture.model.Capture`; see
        :func:`ppk2lab.capture.runner.run_capture` for parameters."""
        from .capture.runner import run_capture

        return run_capture(self, **kwargs)


def _select_device(*, serial_number: str | None, port: str | None) -> DeviceInfo:
    devices = discover()
    if port is not None:
        for dev in devices:
            if any(p.path == port for p in dev.ports):
                return dev
        # A port the discovery scan does not know about: honor it explicitly.
        return DeviceInfo(serial_number=serial_number, vid=None, pid=None, ports=())
    if serial_number is not None:
        for dev in devices:
            if dev.serial_number == serial_number:
                return dev
        raise DeviceNotFoundError(f"no PPK2 with serial number {serial_number!r} found")
    if not devices:
        raise DeviceNotFoundError("no PPK2 devices found")
    if len(devices) > 1:
        serials = ", ".join(str(d.serial_number) for d in devices)
        raise DeviceNotFoundError(
            f"multiple PPK2 devices found ({serials}); select one with --device",
            remediation="Pass --device <serial> to choose a device; run `ppk2lab "
            "discover --json` to list them.",
        )
    return devices[0]
