"""The one thread that owns the device.

A `PPK2` handle is not safe to use from two threads. Only the stream claim is
locked, and that lock exists to *refuse* a second reader rather than to allow
one: the 4-byte sample words carry no sync word, so two readers taking turns on
one port would split words between them and neither would notice. So every call
into the library happens here, on one thread, driven by a command queue --
including reads of ``device.state``, because a state change rebinds a frozen
dataclass and a caller must never see a saga half-applied.

The stream is driven through :class:`~ppk2lab.session.StreamSession` directly
rather than through ``PPK2.stream()``, for a reason that is not the obvious
one. The obvious one is that an unbounded ``stream()`` dies after five seconds
of device silence. The real one is that ``session.events()`` does not yield
while the device is quiet, so a supervisor parked in ``next()`` cannot service
its queue -- a silent device would make "stop the stream" hang indefinitely.
Given a short idle budget, the stall becomes a free liveness tick: ``events()``
has no ``finally``, so raising leaves the session untouched -- the reader thread
still running, the queue and the parser's buffer intact -- and calling it again
resumes with zero bytes written and zero samples lost.

The cost is that the teardown ``PPK2.stream()`` performs has to be performed
here instead, in the same order and for the same reasons. It is
:meth:`Supervisor._stop_stream`, and the order is a safety property rather than
a style.
"""

from __future__ import annotations

import contextlib
import math
import queue
import threading
import time
import uuid
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ppk2lab.diagnostics import as_json
from ppk2lab.errors import StreamStalledError, TransportError
from ppk2lab.protocol.samples import SampleBlock
from ppk2lab.session import StreamSession
from ppk2lab.types import GapEvent, Mode

from . import protocol
from .buckets import Tier0Accumulator
from .codes import (
    ER_RECORDING,
    ER_STATE_MOVED,
    ControlRefused,
    DeviceRefused,
    rejection,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable, Sequence

    from ppk2lab.device import PPK2

    from .buckets import Bucket

#: How long the stream loop will sit in ``next()`` before coming back to serve
#: the queue. Short enough that a stop is prompt, long enough that a healthy
#: stream never reaches it: the device delivers roughly every 10-40 ms.
IDLE_TICK_S = 0.25

#: The device has been quiet for long enough to be worth saying so. Deliberately
#: the library's own default, because that is the number its message quotes.
STALL_WARN_S = 5.0

#: Quiet for long enough that the timeline can no longer be trusted to be
#: continuous. The stream is stopped and the session marked degraded rather
#: than left to accumulate an unknown hole.
STALL_ABORT_S = 30.0

#: Bucket batching. 64 buckets is 64 ms of stream and about 1.6 kB.
FLUSH_BUCKETS = 64
FLUSH_INTERVAL_S = 0.05

#: How often the absolute counters and the distribution grid go out.
COUNTERS_INTERVAL_S = 0.25
HISTOGRAM_INTERVAL_S = 1.0


@dataclass
class Command:
    op: str
    payload: Any = None
    future: Future = field(default_factory=Future)
    origin: str = ""


@dataclass
class ControlRequest:
    """One state change, in the console's vocabulary."""

    kind: str  # "mode" | "voltage" | "dut-power"
    mode: int | None = None
    voltage_mv: int | None = None
    on: bool | None = None

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> ControlRequest:
        kind = raw.get("kind")
        if kind not in ("mode", "voltage", "dut-power"):
            raise ControlRefused("er_unknown", ["BAD_REQUEST", f"unknown control kind {kind!r}"])
        return cls(
            kind=kind,
            mode=raw.get("mode"),
            voltage_mv=raw.get("voltageMv"),
            on=raw.get("on"),
        )

    @property
    def interrupts_stream(self) -> bool:
        """Mode and voltage need the device to stop measuring; DUT power does
        not, and that is what makes an inrush observable live."""
        return self.kind in ("mode", "voltage")


@dataclass
class ApplyPlan:
    changes: list[ControlRequest]
    stop_output_first: bool = False
    restart_output: bool = False

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> ApplyPlan:
        changes = [ControlRequest.from_json(c) for c in raw.get("changes", [])]
        if not changes:
            raise ControlRefused("er_unknown", ["BAD_REQUEST", "a plan changes at least one thing"])
        return cls(
            changes=changes,
            stop_output_first=bool(raw.get("stopOutputFirst")),
            restart_output=bool(raw.get("restartOutput")),
        )

    @property
    def interrupts_stream(self) -> bool:
        return any(c.interrupts_stream for c in self.changes)


class SupervisorError(RuntimeError):
    """The supervisor could not start. Carries the original cause."""


class Supervisor:
    """Owns one open ``PPK2`` for the life of the process."""

    def __init__(
        self,
        *,
        open_device: Callable[[], PPK2],
        publish: Callable[[Any, str | None], None],
        allow_control: bool = False,
        history_seconds: float = 10.0,
        autostart: bool = True,
        idle_tick_s: float = IDLE_TICK_S,
    ) -> None:
        self._open_device = open_device
        self._publish_raw = publish
        self.allow_control = allow_control
        self.history_seconds = history_seconds
        self._autostart = autostart
        self._idle_tick_s = idle_tick_s

        self.session_id = uuid.uuid4().hex[:12]
        self.device: PPK2 | None = None

        self._urgent: queue.SimpleQueue[Command] = queue.SimpleQueue()
        self._normal: queue.SimpleQueue[Command] = queue.SimpleQueue()
        self._stopping = threading.Event()
        self._ready: Future = Future()
        self._thread = threading.Thread(
            target=self._run,
            name="ppk2lab-supervisor",
            # Not a daemon: interpreter exit joins it, which is one of the three
            # independent guarantees that close(restore_power=True) runs.
            daemon=False,
        )

        # Stream state, supervisor-thread only.
        self._session: StreamSession | None = None
        self._events: Any = None
        self._streaming = False
        self._recording = False
        self._timeline_index = 0
        self._acc = Tier0Accumulator()
        self._pending: list[Bucket] = []
        self._discontinuity = True
        self._last_flush = 0.0
        self._last_counters = 0.0
        self._last_histogram = 0.0
        self._last_data = 0.0
        self._stall_warned = False

        # Read by the event loop thread. Replaced wholesale, never mutated in
        # place: a dict rebind is atomic, so a reader either sees the old
        # snapshot or the new one and never a half-written mixture.
        self.snapshot: dict[str, Any] = {}
        self.state_seq = 0
        # Counts configuration moves only. `state_seq` ticks on every republish,
        # including the ones a stop/start of the stream causes, so using it for
        # compare-and-swap would void a preview that nothing relevant had moved.
        self.config_seq = 0
        self._shutdown_done = threading.Event()
        self.close_changes: list[dict[str, Any]] = []
        self.device_present = False

    def set_publisher(self, publish: Callable[[Any, str | None], None]) -> None:
        """Attach the fan-out.

        The hub needs a running event loop and the supervisor needs to exist
        before the app is built, so one of them is wired up second. Naming it
        beats reaching into the other's attributes from `server.py`.
        """
        self._publish_raw = publish

    # -- lifecycle --------------------------------------------------------
    def start(self, *, timeout: float = 20.0) -> None:
        """Open the device and begin. Raises whatever opening raised."""
        self._thread.start()
        self._ready.result(timeout=timeout)

    def shutdown(self, *, timeout: float = 10.0) -> None:
        """Stop, and guarantee the device is closed with its power restored.

        Idempotent, and safe to call from any thread. Three independent callers
        do -- the ASGI lifespan, the supervisor's own ``finally``, and
        ``atexit`` -- because the cost of missing it is a DUT left energised.
        """
        if self._shutdown_done.is_set():
            return
        self._stopping.set()
        self._urgent.put(Command("shutdown"))
        if self._thread.is_alive():
            self._thread.join(timeout=timeout)

    # -- the command interface (any thread) --------------------------------
    def submit(self, op: str, payload: Any = None, *, origin: str = "", urgent: bool = False):
        """Queue a command and return its Future."""
        command = Command(op, payload, origin=origin)
        if self._stopping.is_set():
            command.future.set_exception(ControlRefused("er_offline", ["the server is stopping"]))
            return command.future
        (self._urgent if urgent else self._normal).put(command)
        return command.future

    def request_power_off(self, *, origin: str = "") -> Future:
        """De-energising VOUT never queues behind anything else.

        "Arming asks, disarming does not" is a rule about dialogs in the
        console; here it is a scheduling property, so a power-off cannot sit
        behind a three-second saga.
        """
        return self.submit(
            "apply",
            ControlRequest(kind="dut-power", on=False),
            origin=origin,
            urgent=True,
        )

    # -- the thread -------------------------------------------------------
    def _run(self) -> None:
        try:
            self.device = self._open_device()
        except BaseException as exc:
            # Nothing was opened, so there is nothing to close.
            self._ready.set_exception(exc)
            return

        # From here the handle is live, so every exit runs the shutdown --
        # including a failure in the startup work below. Reporting the failure
        # and returning would leave an open device with its power untouched,
        # and `shutdown()` could not recover it: by then this thread is gone,
        # so its join is a no-op and nothing calls close().
        try:
            self.device_present = True
            self._acc = Tier0Accumulator(start_index=0)
            self._republish_snapshot()
        except BaseException as exc:
            self._ready.set_exception(exc)
            self._shutdown()
            return
        self._ready.set_result(None)

        try:
            if self._autostart:
                with contextlib.suppress(Exception):
                    self._start_stream()
            self._loop()
        finally:
            self._shutdown()

    def _loop(self) -> None:
        while not self._stopping.is_set():
            self._drain(self._urgent)
            if self._stopping.is_set():
                break
            if not self._streaming:
                self._await_command()
                continue
            self._drain_one(self._normal)
            if not self._streaming:
                continue
            try:
                event = next(self._events)
            except StopIteration:
                self._on_stream_ended()
                continue
            except StreamStalledError:
                self._on_idle_tick()
                continue
            except TransportError as exc:
                self._on_device_lost(exc)
                continue
            self._last_data = time.monotonic()
            self._stall_warned = False
            self._ingest(event)
            self._maybe_flush()

    def _await_command(self) -> None:
        try:
            command = self._normal.get(timeout=0.2)
        except queue.Empty:
            return
        self._dispatch(command)

    def _drain(self, lane: queue.SimpleQueue[Command]) -> None:
        while True:
            try:
                command = lane.get_nowait()
            except queue.Empty:
                return
            self._dispatch(command)

    def _drain_one(self, lane: queue.SimpleQueue[Command]) -> None:
        try:
            command = lane.get_nowait()
        except queue.Empty:
            return
        self._dispatch(command)

    def _dispatch(self, command: Command) -> None:
        if command.op == "shutdown":
            self._stopping.set()
            command.future.set_result(None)
            return
        handler = getattr(self, f"_do_{command.op}", None)
        if handler is None:
            command.future.set_exception(
                ControlRefused("er_unknown", ["BAD_REQUEST", f"unknown command {command.op!r}"])
            )
            return
        try:
            command.future.set_result(handler(command))
        except BaseException as exc:
            command.future.set_exception(exc)

    # -- commands ---------------------------------------------------------
    def _do_replay(self, command: Command) -> None:
        """Send a newly attached console the recent stream, so its chart is not
        empty. Only tier 0 is retained; the console folds the coarse tiers."""
        target = command.origin or None
        history = self._acc_backlog()
        if history:
            self._publish(protocol.pack_buckets(history, discontinuity=True), target)
        self._publish(protocol.pack_histogram(self._acc.histogram), target)
        self._publish(self._counters_message(), target)

    def _do_start_stream(self, _command: Command) -> None:
        self._require_device()
        self._start_stream()

    def _do_stop_stream(self, _command: Command) -> None:
        self._stop_stream(reason="request")

    def _do_preview(self, command: Command) -> dict[str, Any]:
        """A dry run of one change. Always allowed: it sends no bytes."""
        self._require_device()
        request: ControlRequest = command.payload
        return self._apply_one(request, dry_run=True).to_json()

    def _do_preview_plan(self, command: Command) -> dict[str, Any]:
        """Project a whole plan without touching the wire.

        A per-request preview cannot say what a plan will do, because the
        *order* is the safety property: dropping the output before the voltage
        moves is the difference between a controlled change and a live one.
        """
        self._require_device()
        plan: ApplyPlan = command.payload
        steps = [self._apply_one(change, dry_run=True).to_json() for change in plan.changes]
        return {
            "steps": steps,
            "interruptsStream": plan.interrupts_stream,
            # The wire name is unchanged; what it carries is the configuration
            # counter, so that starting or stopping the stream between the
            # preview and the confirmation does not invalidate the projection.
            "stateSeq": self.config_seq,
        }

    def _do_apply(self, command: Command) -> dict[str, Any]:
        self._require_control()
        self._require_device()
        request: ControlRequest = command.payload
        if request.kind != "dut-power" or request.on:
            self._require_not_recording()
        change = self._apply_one(request, dry_run=False)
        self._republish_snapshot(change.to_json())
        return change.to_json()

    def _do_apply_plan(self, command: Command) -> dict[str, Any]:
        self._require_control()
        self._require_device()
        self._require_not_recording()
        plan, expected_seq = command.payload
        if expected_seq is not None and expected_seq != self.config_seq:
            # Compare-and-swap. Without it, a console that previewed, lost its
            # connection, came back and pressed Confirm would apply a plan
            # against a device in a different state than the one it showed.
            raise ControlRefused(ER_STATE_MOVED, [expected_seq, self.config_seq])
        return {"steps": self._apply_plan(plan)}

    def _do_record(self, command: Command) -> dict[str, Any]:
        self._require_control()
        self._require_device()
        self._require_not_recording()
        return self._record(**command.payload)

    # -- guards -----------------------------------------------------------
    def _require_device(self) -> None:
        if self.device is None or not self.device_present:
            raise ControlRefused("er_offline", ["the device is no longer attached"])

    def _require_control(self) -> None:
        if not self.allow_control:
            # Structural, not cosmetic: without --allow-control the app never
            # registers a handler that reaches here, and this is the second
            # gate behind that one.
            raise ControlRefused("er_locked")

    def _require_not_recording(self) -> None:
        if self._recording:
            raise ControlRefused(ER_RECORDING)

    # -- state changes ----------------------------------------------------
    def _apply_one(self, request: ControlRequest, *, dry_run: bool):
        device = self.device
        assert device is not None
        try:
            if request.kind == "mode":
                if request.mode is None:
                    raise ControlRefused(
                        "er_unknown", ["BAD_REQUEST", "a mode change needs a mode"]
                    )
                return device.set_mode(Mode(request.mode), dry_run=dry_run)
            if request.kind == "voltage":
                assert request.voltage_mv is not None
                return device.set_source_voltage_mv(request.voltage_mv, dry_run=dry_run)
            assert request.on is not None
            return device.set_dut_power(request.on, dry_run=dry_run)
        except Exception as exc:
            raise DeviceRefused(exc, requested_mv=request.voltage_mv) from exc

    def _apply_plan(self, plan: ApplyPlan) -> list[dict[str, Any]]:
        """Stop the output, stop measuring, apply, resume, re-arm.

        The order is the safety property, and it is the console's order because
        the console's order is the correct one: changing the source voltage
        while VOUT is energised changes what the DUT receives, live.
        """
        device = self.device
        assert device is not None

        # Validate everything before a single byte reaches the wire, so a plan
        # that will be refused leaves the device exactly as it was.
        for change in plan.changes:
            self._apply_one(change, dry_run=True)

        applied: list[dict[str, Any]] = []
        was_streaming = self._streaming
        self._publish(protocol.stream(running=self._streaming, phase="applying"))

        # Every step is broadcast as it lands rather than the plan reporting
        # once at the end: a saga that stops the stream and writes three
        # commands takes seconds, and a console showing the old state for the
        # whole of it is showing hardware that has already moved. The `state`
        # message carries the change, so no separate `event` is published --
        # that would enter the same step in the console's log twice.
        if plan.stop_output_first and device.state.dut_power:
            step = self._apply_one(ControlRequest("dut-power", on=False), dry_run=False).to_json()
            applied.append(step)
            self._republish_snapshot(step)
        if plan.interrupts_stream and was_streaming:
            self._stop_stream(reason="control")

        try:
            for change in plan.changes:
                step = self._apply_one(change, dry_run=False).to_json()
                applied.append(step)
                self._republish_snapshot(step)
        except BaseException:
            # Restoring the stream is owed to the operator. Restoring power is
            # not: a plan that failed must never leave VOUT energised because
            # its last step said to.
            if plan.interrupts_stream and was_streaming:
                with contextlib.suppress(Exception):
                    self._start_stream()
            self._republish_snapshot()
            # The "applying" phase was published on the way in, so leaving
            # without replacing it strands every console in it forever.
            self._publish(protocol.stream(running=self._streaming, phase=self._phase()))
            raise
        if plan.interrupts_stream and was_streaming:
            self._start_stream()
        if plan.restart_output:
            step = self._apply_one(ControlRequest("dut-power", on=True), dry_run=False).to_json()
            applied.append(step)
            self._republish_snapshot(step)

        self._publish(protocol.stream(running=self._streaming, phase=self._phase()))
        return applied

    # -- stream -----------------------------------------------------------
    def _start_stream(self) -> None:
        if self._streaming:
            return
        device = self.device
        assert device is not None
        session = StreamSession(device.transport, start_index=self._timeline_index)
        device.last_session = session
        device.start_measuring()
        session.start()
        self._session = session
        self._events = session.events(
            sample_limit=None, wall_timeout_s=None, idle_timeout_s=self._idle_tick_s
        )
        self._streaming = True
        self._discontinuity = True
        self._last_data = time.monotonic()
        self._stall_warned = False
        self._republish_snapshot()
        self._publish(protocol.stream(running=True, phase="running"))

    def _stop_stream(self, *, reason: str) -> None:
        """Give the device back, in the order ``PPK2.stream()`` uses.

        The claim ``PPK2`` would hold is replaced by this supervisor being the
        only thing that touches the handle, but the rest of the teardown is not
        optional and its order is not a style: the reader stops, then the
        measurement stops, then the port is drained, and only then is the
        handle idle. The drain matters because bytes already in flight when the
        stop lands would otherwise be parsed as the head of the next session,
        silently shifting its framing.
        """
        if not self._streaming:
            return
        self._streaming = False
        session, events = self._session, self._events
        self._session, self._events = None, None
        device = self.device

        try:
            if session is not None:
                session.stop()
                # Drain what is already parsed rather than dropping the
                # generator. `events()` yields from the list `parser.feed()`
                # returned, so abandoning it mid-list silently discards
                # SampleBlocks that were parsed and never delivered -- loss
                # this project would then report as the instrument's.
                self._drain_events(events)
                self._timeline_index = session.parser.next_index
        finally:
            for bucket in self._acc.flush():
                self._pending.append(bucket)
            self._flush(force=True)
            if device is not None:
                try:
                    device.stop_measuring()
                except Exception as exc:
                    self._warn("W_STATE_UNVERIFIED", f"stop_measuring failed: {exc}")
                with contextlib.suppress(Exception):
                    device.stale_bytes_after_stop = _drain_port(device)
            self._acc.break_continuity()
            self._discontinuity = True
            self._republish_snapshot()
            self._publish(protocol.stream(running=False, phase="stopped", reason=reason))

    def _drain_events(self, events: Any, *, budget_s: float = 3.0) -> None:
        if events is None:
            return
        deadline = time.monotonic() + budget_s
        while time.monotonic() < deadline:
            try:
                event = next(events)
            except StopIteration:
                return
            except StreamStalledError:
                # The reader has stopped, so the sentinel is already queued;
                # a stall here only means the queue emptied between two gets.
                continue
            except Exception:
                return
            self._ingest(event)

    def _on_stream_ended(self) -> None:
        self._stop_stream(reason="ended")

    def _on_idle_tick(self) -> None:
        """The device has been quiet for one tick. Nothing is torn down.

        `events()` has no `finally`, so the session survives untouched and the
        next call resumes it: no bytes written, no samples lost, no
        discontinuity. What escalates is only the reporting.
        """
        session = self._session
        if session is None:
            return
        quiet = time.monotonic() - self._last_data
        if quiet > STALL_ABORT_S:
            self._acc.timeline_degraded = True
            self._warn("W_STREAM_DESYNC", f"no data for {quiet:.0f} s; the stream was stopped")
            self._stop_stream(reason="stalled")
            return
        if quiet > STALL_WARN_S and not self._stall_warned:
            self._stall_warned = True
            self._warn("W_SAMPLE_GAPS", f"no data from the device for {quiet:.0f} s")
            self._publish(protocol.stream(running=True, phase="stalled"))
        self._events = session.events(
            sample_limit=None, wall_timeout_s=None, idle_timeout_s=self._idle_tick_s
        )

    def _on_device_lost(self, exc: BaseException) -> None:
        """A hot unplug, or any transport failure.

        The HTTP server keeps running: a page that can show what happened and
        what to do about it is worth more than a process that exits. The device
        is closed anyway -- `close()` tolerates a dead port and records what it
        could not do rather than skipping it silently.
        """
        self.device_present = False
        self._streaming = False
        self._session, self._events = None, None
        # Samples that were converted but not yet sent are still measurements.
        # Dropping them here would lose the moment before the cable came out,
        # which is the part of an unplug anyone would want to look at.
        for bucket in self._acc.flush():
            self._pending.append(bucket)
        self._flush(force=True)
        self._warn("W_INTERRUPTED", str(exc))
        self._publish(protocol.error(rejection(exc)))
        self._publish(protocol.stream(running=False, phase="stopped", reason="device_lost"))
        device = self.device
        if device is not None:
            with contextlib.suppress(Exception):
                self.close_changes = [c.to_json() for c in device.close(restore_power=True)]
                for change in self.close_changes:
                    # One publishing path. The handle is still here, so this
                    # reads cached fields and sends no bytes to a dead port.
                    self._republish_snapshot(change)
        self._republish_snapshot()

    # -- recording --------------------------------------------------------
    def _record(self, **kwargs: Any) -> dict[str, Any]:
        """Write a capture artifact while the console keeps drawing.

        `run_capture` opens its own stream, so this one comes down first. The
        live view is then fed from the capture's own blocks through `on_block`,
        which means the trace and the artifact cannot disagree -- they are the
        same objects.

        `on_block` is also where the urgent lane is served, because it is the
        only point between two reads of a 100 kS/s device. A recording cannot
        be cancelled -- `run_capture` has no cooperative stop -- so it is bound
        by `duration_s` or `sample_limit`, and the console says so before it
        starts.
        """
        from ppk2lab.capture.runner import run_capture

        device = self.device
        assert device is not None
        was_streaming = self._streaming
        self._stop_stream(reason="record")
        self._recording = True
        self._publish(protocol.stream(running=True, phase="recording"))

        def on_block(event: SampleBlock | GapEvent) -> None:
            self._ingest(event)
            self._maybe_flush()
            # Power-off must still work while a recording runs. It is the one
            # state change the device permits mid-stream, and the one an
            # operator may need most.
            self._drain(self._urgent)

        try:
            result = run_capture(device, on_block=on_block, **kwargs)
        finally:
            self._recording = False
            self._timeline_index = self._acc.next_index
            if was_streaming:
                with contextlib.suppress(Exception):
                    self._start_stream()
            else:
                self._publish(protocol.stream(running=False, phase="stopped", reason="record"))

        # A capture without a stopping condition is refused by run_capture, so
        # stats are always present here; the guard is for the type checker and
        # for the day that stops being true.
        stats = result.stats
        stored = stats.stored_samples if stats is not None else 0
        self._publish(
            protocol.event(
                kind="info",
                operation="capture",
                message_key="ev_capture",
                args=[str(result.path), f"{stored:,} samples"],
                index=self._acc.next_index,
            )
        )
        return {
            "path": str(result.path) if result.path else None,
            "complete": result.complete,
            "stored_samples": stored,
            "gap_count": stats.gap_count if stats is not None else 0,
            "warnings": as_json(result.warnings),
            "capture_sha256": result.capture_sha256,
        }

    # -- sample path ------------------------------------------------------
    def _ingest(self, event: SampleBlock | GapEvent) -> None:
        if isinstance(event, SampleBlock):
            device = self.device
            calibration = device.calibration if device is not None else None
            if calibration is None:
                currents: Sequence[float] = [math.nan] * len(event)
            else:
                currents = calibration.convert_block(event, self._vdd_mv)
            self._pending.extend(self._acc.add_samples(currents, event.ranges, event.logic))
            return
        # Taken before add_gap, and from the accumulator rather than the event:
        # a recording drives its own stream whose indices restart at zero, so
        # `event.index` would place the gap somewhere this console never was.
        at = self._acc.position
        self._pending.extend(self._acc.add_gap(event.missing))
        self._publish(
            protocol.gap(
                index=at,
                missing=event.missing,
                reason=event.reason,
                ambiguous=event.ambiguous,
            )
        )

    def _maybe_flush(self) -> None:
        now = time.monotonic()
        if len(self._pending) >= FLUSH_BUCKETS or now - self._last_flush >= FLUSH_INTERVAL_S:
            self._flush()
        if now - self._last_counters >= COUNTERS_INTERVAL_S:
            self._last_counters = now
            self._publish(self._counters_message())
        if now - self._last_histogram >= HISTOGRAM_INTERVAL_S:
            self._last_histogram = now
            self._publish(protocol.pack_histogram(self._acc.histogram))

    def _flush(self, *, force: bool = False) -> None:
        if not self._pending:
            if force:
                self._last_flush = time.monotonic()
            return
        frame = protocol.pack_buckets(self._pending, discontinuity=self._discontinuity)
        self._discontinuity = False
        self._pending = []
        self._last_flush = time.monotonic()
        self._publish(frame)

    def _acc_backlog(self) -> list[Bucket]:
        # The retained history lives in the console's rings, not here; what a
        # newly attached console gets is whatever has not been sent yet plus
        # the counters, and the honest limit is stated in docs/webui.md.
        return list(self._pending)

    # -- publishing -------------------------------------------------------
    def _publish(self, message: Any, target: str | None = None) -> None:
        self._publish_raw(message, target)

    def _counters_message(self) -> dict[str, Any]:
        session = self._session
        return protocol.counters(
            total_stored=self._acc.total_stored,
            total_missing=self._acc.total_missing,
            total_excluded=self._acc.total_excluded,
            gap_count=self._acc.gap_count,
            timeline_degraded=self._acc.timeline_degraded,
            peak_queued_bytes=session.peak_queued_bytes if session else 0,
            index=self._acc.next_index,
        )

    def _warn(self, code: str, message: str) -> None:
        self._publish(
            protocol.event(
                kind="warn",
                operation=code,
                message_key="w_unknown",
                args=[code, message],
                index=self._acc.next_index,
            )
        )

    def _bump_seq(self) -> int:
        self.state_seq += 1
        return self.state_seq

    def _config(self) -> dict[str, Any]:
        device = self.device
        if device is None:
            return {"mode": None, "voltageMv": None}
        return {
            "mode": int(device.state.mode) if device.state.mode is not None else None,
            "voltageMv": device.state.source_voltage_mv,
        }

    def _phase(self) -> str:
        if self._recording:
            return "recording"
        return "running" if self._streaming else "stopped"

    def _republish_snapshot(self, change: dict[str, Any] | None = None) -> None:
        device = self.device
        if device is None:
            return
        fingerprint = None
        with contextlib.suppress(Exception):
            fp = device.firmware_fingerprint()
            fingerprint = f"HW={fp['hw']} IA={fp['ia']} keys={fp['metadata_key_count']}"
        state = device.state.to_json()
        self.snapshot = {
            "device": protocol.device_json(device.info, fingerprint),
            "state": state,
            "calibration": protocol.calibration_json(device.calibration, device.metadata),
            "counters": self._counters_message(),
            "streaming": self._streaming,
            "config": self._config(),
            "max_voltage_mv": self.max_voltage_mv,
            "attach_index": self._acc.next_index,
            "warnings": as_json(device.metadata.warnings if device.metadata else []),
        }
        seq = self._bump_seq()
        if change is not None:
            self.config_seq += 1
        # Always broadcast, `change` or not. A transition with no single cause
        # to name -- a stream stopping, a device closing itself -- still moves
        # the state every console is showing, and the console that only learns
        # of it by reconnecting is the bug this guards against.
        self._publish(
            protocol.state(device_state=state, change=change, state_seq=seq, config=self._config())
        )

    @property
    def max_voltage_mv(self) -> int | None:
        """The session ceiling, read from the device that enforces it.

        Not a copy taken at construction: `PPK2` resolves it from its own
        argument or from PPK2LAB_MAX_VOLTAGE_MV, and a second copy here could
        only ever be a second place for it to be wrong. It is reported to the
        console so a refusal can name the number, and the console may lower it
        for its own session but never raise it.
        """
        device = self.device
        return device.max_voltage_mv if device is not None else None

    @property
    def _vdd_mv(self) -> float | None:
        """The voltage every conversion uses, read from the device.

        Not cached: ``set_source_voltage_mv`` updates the device's own state,
        and a copy here could only ever be a second place for it to be wrong.
        """
        device = self.device
        if device is None:
            return None
        value = device.state.source_voltage_mv
        return float(value) if value is not None else None

    # -- shutdown ---------------------------------------------------------
    def _shutdown(self) -> None:
        if self._shutdown_done.is_set():
            return
        try:
            with contextlib.suppress(Exception):
                self._stop_stream(reason="shutdown")
            device = self.device
            if device is not None:
                try:
                    self.close_changes = [c.to_json() for c in device.close(restore_power=True)]
                except Exception as exc:
                    self.close_changes = [{"operation": "close", "error": str(exc)}]
            with contextlib.suppress(Exception):
                self._publish(protocol.bye(reason="shutdown"))
        finally:
            self._shutdown_done.set()


def _drain_port(device: PPK2) -> int | None:
    from ppk2lab.device import _drain_input

    try:
        return _drain_input(device.transport, max_seconds=0.5, quiet_reads=2)
    except Exception:
        # A dead handle drains nothing, and "nothing" is not zero verified-quiet
        # bytes: record the drain as not performed rather than leaving the
        # previous count standing.
        return None
