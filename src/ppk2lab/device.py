"""Synchronous PPK2 device API.

Every state-changing operation returns a :class:`~ppk2lab.types.StateChange`
with requested/before/after state and whether the "after" state was actually
read back from the device. Nothing here ever enables DUT power implicitly.
"""

from __future__ import annotations

import contextlib
import os
import sys
import threading
import time
import weakref
from collections.abc import Generator, Iterator
from dataclasses import replace
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
from .session import DEFAULT_IDLE_TIMEOUT_S, StreamSession
from .transport.base import Transport
from .transport.mock import MockTransport, SimulatedPPK2
from .transport.serial import SerialTransport
from .types import DeviceInfo, DeviceState, Mode, PortRole, StateChange, VoltageBasis

#: Factory for real serial transports; tests monkeypatch this to exercise
#: the measurement-port probing path without hardware.
_transport_factory = SerialTransport

#: Environment variable holding a session-wide source-voltage ceiling in mV.
MAX_VOLTAGE_ENV = "PPK2LAB_MAX_VOLTAGE_MV"
#: Set to "1" to make every open simulate, without editing the call site.
#: Honoured here rather than only in the CLI so a script and a command behave
#: the same way under it, as they already do for MAX_VOLTAGE_ENV.
SIMULATE_ENV = "PPK2LAB_SIMULATE"


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
    """True once the metadata reply carries ``END`` at a line start.

    Requiring the newline makes stray bytes that happen to spell ``END``
    (binary residue from an interrupted stream) far less likely to truncate
    an otherwise complete reply. It is a substring search over the buffer,
    not a guarantee: residue containing ``\nEND`` would still match.
    """
    return buf.startswith(b"END") or b"\nEND" in buf or b"\rEND" in buf


def _env_simulate() -> bool:
    return os.environ.get(SIMULATE_ENV) == "1"


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


class StreamIterator(Iterator[SampleBlock | GapEvent]):
    """Closable iterator over one device's stream events.

    Iterating it is exactly iterating the stream; what it adds is a close
    path the caller controls::

        with device.stream(duration_s=1.0) as events:
            for event in events:
                ...

    That matters because the stream owns hardware, not just memory: an
    iterator kept alive by a stored exception traceback keeps the device
    claimed *and keeps the PPK2 measuring* until the traceback is dropped.
    Bare iteration still works and still cleans up when the iterator is
    finalized; only the ``with`` form is prompt regardless of who holds a
    reference.
    """

    def __init__(self, device: PPK2, events: Generator[SampleBlock | GapEvent, None, None]) -> None:
        self._device = device
        self._events = events
        self._closed = False

    def __iter__(self) -> StreamIterator:
        return self

    def __next__(self) -> SampleBlock | GapEvent:
        return next(self._events)

    def close(self) -> None:
        """Stop measuring and release the device claim. Idempotent."""
        if self._closed:
            return
        self._closed = True
        try:
            self._events.close()
        finally:
            self._release_claim()

    def _release_claim(self) -> None:
        """Give the device back, but only if this stream still holds it.

        A generator that was never started does not run its own finally
        block, so the claim ``stream()`` takes before the first iteration has
        to be released here too. The identity check keeps that from reaching
        too far: an exhausted iterator closed or collected after a later
        stream began must not release *that* stream's claim.
        """
        active = self._device._active_stream
        if active is not None and active() is self:
            self._device._stream_active = False
            self._device._active_stream = None

    def __enter__(self) -> StreamIterator:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __del__(self) -> None:
        # The last resort for an iterator that is simply dropped. It matters
        # most for one that was never iterated at all: its generator never
        # ran, so no finally block of its own will ever release the claim
        # stream() took, and every later command on the handle would be
        # refused for a stream that never started. Nothing here can be
        # reported to anyone, so failures are discarded rather than printed
        # as an "Exception ignored" traceback the caller cannot act on.
        with contextlib.suppress(Exception):
            self.close()


class PPK2:
    """One open PPK2 measurement session."""

    def __init__(
        self, transport: Transport, info: DeviceInfo, *, max_voltage_mv: int | None = None
    ) -> None:
        self.transport = transport
        self.info = info
        self._state = DeviceState()
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
        #: The stream handed to the caller, tracked weakly: a strong
        #: reference would make the device and the generator hold each
        #: other, so a dropped iterator would wait for the cycle collector
        #: instead of being finalized — and stop measuring — at once.
        self._active_stream: weakref.ref[StreamIterator] | None = None
        #: The most recent streaming session, kept so a capture can report
        #: what its parser observed (desyncs, truncated gap tables).
        self.last_session: StreamSession | None = None
        #: Bytes discarded after the last stop command, or ``None`` when no
        #: drain has completed on this handle. The distinction is the whole
        #: point of the field: a dead port drains nothing, so recording 0
        #: there would be indistinguishable from "the channel was read until
        #: it stayed quiet", which is a positive verification nobody made.
        self.stale_bytes_after_stop: int | None = None

    @property
    def state(self) -> DeviceState:
        """Host-tracked device state; read-only, see :class:`DeviceState`."""
        return self._state

    def _update_state(self, **fields: Any) -> None:
        """Record state the device has been commanded into.

        The only writer of :attr:`state`. Every caller is a code path that
        has already put the command on the wire (or read the value back),
        which is what keeps a capture manifest's ``dut_power`` an account of
        what this session did rather than an assertion anyone can make.
        """
        self._state = replace(self._state, **fields)

    # -- guards ------------------------------------------------------------
    def _require_no_active_stream(self, action: str) -> None:
        if self._stream_active:
            raise UsageError(
                f"cannot {action} while a capture stream is active on this device",
                remediation="Let the active stream finish, or close it explicitly — "
                "`with device.stream(...) as events:` releases the device even when "
                "the block exits on an exception, whereas a named iterator held alive "
                "by a stored traceback keeps it claimed. One device, one stream.",
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
        simulate: bool | None = None,
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

        ``simulate=None`` (the default) reads ``PPK2LAB_SIMULATE``; pass
        ``True`` or ``False`` to decide explicitly regardless of it. The
        variable never applies to an injected ``transport``: the environment
        must not decide the identity of a transport the caller supplied, or a
        real instrument gets labelled ``simulated: true`` in a stored manifest
        while every byte still reaches the wire.
        """
        if transport is not None:
            info = (
                simulated_device_info("injected")
                if simulate is True
                else DeviceInfo(
                    serial_number=serial_number, vid=None, pid=None, ports=(), simulated=False
                )
            )
        elif (simulate if simulate is not None else _env_simulate()) or simulator is not None:
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
        self._update_state(measuring=False)
        return self.recovered_stale_bytes

    def close(self, *, restore_power: bool = True) -> list[StateChange]:
        """Close the session, restoring the session-start DUT power state.

        Restoration is best-effort: failures are recorded in the returned
        state changes rather than raised, and the transport always closes.
        """
        if self._closed:
            return []
        self._close_active_stream()
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

    def _close_active_stream(self) -> None:
        """Close a still-open stream before the transport goes away.

        The stream's own cleanup writes a stop command and drains the
        channel, so it has to run while the port is still open; left to the
        garbage collector it would run against a closed transport and only
        raise. Failures are ignored: at this point the caller has already
        stopped consuming the stream, so nothing measured is at stake, and
        the transport must close regardless.
        """
        ref = self._active_stream
        stream = ref() if ref is not None else None
        if stream is not None:
            with contextlib.suppress(Exception):
                stream.close()
        self._active_stream = None

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
            self._update_state(mode=Mode(metadata.mode))
        if metadata.vdd_mv is not None:
            self._update_state(source_voltage_mv=metadata.vdd_mv)
            # A readback after this session set the voltage keeps the stronger
            # basis; otherwise the value is just the regulator's setpoint.
            if not self._voltage_configured:
                self._update_state(source_voltage_basis=VoltageBasis.DEVICE_METADATA)
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
            self._update_state(mode=mode)
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
            self._update_state(
                source_voltage_mv=voltage_mv,
                source_voltage_basis=VoltageBasis.CONFIGURED_SOURCE,
            )
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
            # Drain before reporting this applied. Every other state-changing
            # command proves delivery with a readback; this one has none, so a
            # byte left in the OS write buffer would be reported as applied
            # while the hardware never saw it. A short-lived CLI run is covered
            # by the drain in close(), but a long-lived session -- the web
            # console holds one open for hours -- has nothing else that would
            # ever flush it.
            self.transport.flush()
            self._update_state(dut_power=on)
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
            self._state = DeviceState()  # all state unknown after reset
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
        self._update_state(measuring=True)

    def stop_measuring(self) -> None:
        self.transport.write(cmd_stop_measuring())
        self._update_state(measuring=False)

    def stream(
        self,
        *,
        sample_limit: int | None = None,
        duration_s: float | None = None,
        wall_timeout_s: float | None = None,
        idle_timeout_s: float | None = DEFAULT_IDLE_TIMEOUT_S,
    ) -> StreamIterator:
        """Start measuring and yield loss-aware stream events.

        Stops measuring when the iterator is closed or exhausted. Claiming the
        device happens here rather than inside the generator body, so the
        claim is made when the caller asks for the stream and not deferred to
        whenever they first iterate it.

        The returned :class:`StreamIterator` is a context manager, and that
        is the documented idiom — a stream owns the instrument, so releasing
        it should not depend on when the iterator happens to be collected::

            with device.stream(duration_s=1.0) as events:
                for event in events:
                    ...

        ``idle_timeout_s`` caps the silence tolerated between two byte
        deliveries. The device sends continuously while measuring, so seconds
        of silence means it stopped -- a state that raises nothing on its own.
        The default matches :data:`~ppk2lab.session.DEFAULT_IDLE_TIMEOUT_S`.
        ``None`` disables the check, which only a caller with its own liveness
        signal should choose: without it, a device that goes quiet with the
        port still open blocks the consumer forever.
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
        with self._lock:
            # Check-and-claim must be atomic, or two threads both see an idle
            # device and interleave reads into one 4-byte framing stream.
            self._require_no_active_stream("start another stream")
            self._stream_active = True
        session = StreamSession(self.transport)
        self.last_session = session
        iterator = StreamIterator(
            self, self._stream_events(session, sample_limit, wall_timeout_s, idle_timeout_s)
        )
        self._active_stream = weakref.ref(iterator)
        return iterator

    def _stream_events(
        self,
        session: StreamSession,
        sample_limit: int | None,
        wall_timeout_s: float | None,
        idle_timeout_s: float | None,
    ) -> Generator[SampleBlock | GapEvent, None, None]:
        try:
            # start_measuring and session.start are inside the try: if either
            # raises, the claim must still be released or the handle is stuck
            # rejecting every later command for a stream that never began.
            self.start_measuring()
            session.start()
            yield from session.events(
                sample_limit=sample_limit,
                wall_timeout_s=wall_timeout_s,
                idle_timeout_s=idle_timeout_s,
            )
        finally:
            # Whatever ended the stream — a hot unplug, a stall, the
            # caller's own error — is what the caller has to see. Read it
            # before any nested handler below can clear it.
            in_flight = sys.exception()
            # The claim is given back last, from a finally of its own. It is
            # what keeps a second stream off this handle, and the handle is
            # not free until the measurement has stopped and the port has
            # been drained. Clearing it first left a window — up to the
            # reader join below — in which stream() was admitted, called
            # start_measuring(), and was then silently stopped again by the
            # stop_measuring() here, or had the head of its framing eaten by
            # the drain. The window is not theoretical: this block is run by
            # StreamIterator.__del__, and a collection runs on whichever
            # thread filled a generation, so on Linux CI it was recorded
            # running on an asyncio worker while another thread read the
            # handle as idle.
            try:
                session.stop()
                try:
                    self.stop_measuring()
                except Exception:
                    self._update_state(measuring=False)
                # Bytes already in flight when the stop command lands would
                # otherwise be parsed as the head of the next session on this
                # handle, silently shifting its framing.
                try:
                    self.stale_bytes_after_stop = _drain_input(
                        self.transport, max_seconds=0.5, quiet_reads=2
                    )
                except Exception:
                    # A dead handle drains nothing, and "nothing" is not zero
                    # verified-quiet bytes: record the drain as not performed
                    # rather than leaving the previous stream's count standing.
                    # The failure is only reportable when the stream ended
                    # cleanly — during an unwind it would replace the real cause
                    # with a symptom.
                    self.stale_bytes_after_stop = None
                    if in_flight is None:
                        raise
            finally:
                self._stream_active = False

    def capture(self, **kwargs: Any):
        """Capture to a :class:`~ppk2lab.capture.model.Capture`; see
        :func:`ppk2lab.capture.runner.run_capture` for parameters."""
        from .capture.runner import run_capture

        return run_capture(self, **kwargs)


def _select_device(*, serial_number: str | None, port: str | None) -> DeviceInfo:
    # Explicitly not simulated: this is only reached once `open()` has decided
    # it wants real hardware, and a bare `discover()` would read
    # PPK2LAB_SIMULATE and enumerate the simulator instead — so an explicit
    # `simulate=False` could not escape the variable it is meant to override.
    devices = discover(simulate=False)
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
