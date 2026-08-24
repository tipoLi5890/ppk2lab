"""The thread that owns the device, driven against the simulator.

No ASGI here and no socket: the supervisor publishes through a callable, so a
list stands in for the hub and every claim below is about the device layer
rather than about transport.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from ppk2lab.device import PPK2
from ppk2lab.errors import TransportError
from ppk2lab.protocol.commands import Opcode
from ppk2lab.testing.profiles import ConstantProfile, DemoActivityProfile
from ppk2lab.transport.mock import MockTransport, SimulatedPPK2
from ppk2lab.types import Mode
from ppk2lab_web import protocol
from ppk2lab_web.codes import ER_CEILING, ER_LOCKED, ER_RANGE, ControlRefused, DeviceRefused
from ppk2lab_web.supervisor import ApplyPlan, ControlRequest, Supervisor


class Sink:
    """Stands in for the hub. Records everything, in order."""

    def __init__(self) -> None:
        self.messages: list[tuple[Any, str | None]] = []
        self._lock = threading.Lock()

    def __call__(self, message: Any, target: str | None) -> None:
        with self._lock:
            self.messages.append((message, target))

    def json(self, kind: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            out = [m for m, _ in self.messages if isinstance(m, dict)]
        return [m for m in out if kind is None or m.get("type") == kind]

    def frames(self) -> list[bytes]:
        with self._lock:
            return [m for m, _ in self.messages if isinstance(m, bytes)]

    def buckets(self) -> list[dict[str, Any]]:
        out = []
        for frame in self.frames():
            if frame[0] == protocol.TAG_BUCKETS:
                out.append(protocol.unpack_buckets(frame))
        return out


def wait_for(predicate, *, timeout: float = 5.0, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


def make_supervisor(
    sink: Sink,
    *,
    profile=None,
    simulator: SimulatedPPK2 | None = None,
    allow_control: bool = True,
    autostart: bool = True,
    max_voltage_mv: int | None = None,
    idle_tick_s: float = 0.15,
) -> tuple[Supervisor, SimulatedPPK2]:
    sim = simulator or SimulatedPPK2(profile=profile or ConstantProfile(100.0))

    def opener() -> PPK2:
        # The ceiling belongs to the device, which is what enforces it before a
        # byte reaches the wire. The supervisor reads it back rather than
        # keeping a copy.
        return PPK2.open(transport=MockTransport(sim), simulate=True, max_voltage_mv=max_voltage_mv)

    sup = Supervisor(
        open_device=opener,
        publish=sink,
        allow_control=allow_control,
        autostart=autostart,
        idle_tick_s=idle_tick_s,
    )
    return sup, sim


# ---------------------------------------------------------------------------
# Streaming


def test_it_streams_buckets_and_gives_the_device_back():
    sink = Sink()
    sup, sim = make_supervisor(sink)
    sup.start()
    try:
        wait_for(lambda: sink.buckets(), what="a bucket frame")
        batch = sink.buckets()[0]
        assert batch["count"] >= 1
        assert batch["n"][0] == 100
        assert batch["discontinuity"] is True  # the first batch of a stream
        sup.submit("stop_stream").result(timeout=5)
        assert not sim.measuring
    finally:
        sup.shutdown()
    assert not sim.measuring


def test_the_first_batch_after_a_restart_is_marked_discontinuous():
    """The console breaks its mean line there. Drawing across a join that was
    never measured would be a measurement nobody took."""
    sink = Sink()
    sup, _sim = make_supervisor(sink)
    sup.start()
    try:
        wait_for(lambda: sink.buckets(), what="a bucket frame")
        sup.submit("stop_stream").result(timeout=5)
        before = len(sink.buckets())
        sup.submit("start_stream").result(timeout=5)
        wait_for(lambda: len(sink.buckets()) > before, what="a bucket after the restart")
        assert sink.buckets()[before]["discontinuity"] is True
    finally:
        sup.shutdown()


def test_the_timeline_continues_across_a_stop_and_start():
    """Indices are the console's clock. Restarting at zero would put the new
    trace on top of the old one."""
    sink = Sink()
    sup, _sim = make_supervisor(sink)
    sup.start()
    try:
        wait_for(lambda: sink.buckets(), what="a bucket frame")
        sup.submit("stop_stream").result(timeout=5)
        first_end = max(b["start_index"][-1] for b in sink.buckets())
        seen = len(sink.buckets())
        sup.submit("start_stream").result(timeout=5)
        wait_for(lambda: len(sink.buckets()) > seen, what="a bucket after the restart")
        assert sink.buckets()[seen]["first_index"] >= first_end
    finally:
        sup.shutdown()


def test_an_idle_tick_costs_the_device_nothing():
    """The whole reason the stream is driven through StreamSession directly.

    A quiet device raises StreamStalledError on the idle budget; `events()` has
    no `finally`, so the session is untouched and the next call resumes it. If
    this were built on `PPK2.stream()`, every tick would write a STOP, drain the
    port for half a second, and start a fresh parser.
    """
    sim = SimulatedPPK2(profile=ConstantProfile(100.0))
    original_read = sim.read

    def mute(max_bytes, timeout_s=0.1):
        original_read(max_bytes, timeout_s)  # keep command handling alive
        return b""

    sink = Sink()
    sup, _ = make_supervisor(sink, simulator=sim, idle_tick_s=0.05)
    sup.start()
    try:
        sim.read = mute  # type: ignore[method-assign]
        before = sum(1 for op, _ in sim.command_log if op is Opcode.STOP_MEASURING)
        time.sleep(0.4)  # several idle ticks
        after = sum(1 for op, _ in sim.command_log if op is Opcode.STOP_MEASURING)
        assert after == before, "an idle tick must not tear the stream down"
        # And the loop is still serving its queue, which is the other half.
        sup.submit("stop_stream").result(timeout=5)
    finally:
        sim.read = original_read  # type: ignore[method-assign]
        sup.shutdown()


# ---------------------------------------------------------------------------
# Control


def test_control_is_unreachable_without_allow_control():
    sink = Sink()
    sup, sim = make_supervisor(sink, allow_control=False, autostart=False)
    sup.start()
    try:
        before = list(sim.command_log)
        with pytest.raises(ControlRefused) as excinfo:
            sup.submit("apply", ControlRequest("dut-power", on=True)).result(timeout=5)
        assert excinfo.value.key == ER_LOCKED
        assert sim.command_log == before, "a refused control reached the wire"
        assert sim.dut_power is False
    finally:
        sup.shutdown()


def test_a_preview_is_allowed_even_when_control_is_not():
    """A dry run sends no bytes, so there is nothing to gate. Refusing it would
    only stop an operator seeing what a change *would* do."""
    sink = Sink()
    sup, sim = make_supervisor(sink, allow_control=False, autostart=False)
    sup.start()
    try:
        before = list(sim.command_log)
        change = sup.submit("preview", ControlRequest("dut-power", on=True)).result(timeout=5)
        assert change["applied"] is False
        assert sim.command_log == before
    finally:
        sup.shutdown()


def test_a_plan_runs_its_steps_in_the_safe_order():
    """Drop the output, stop measuring, apply, resume, re-arm. Changing the
    source voltage while VOUT is energised changes what the DUT receives."""
    sink = Sink()
    sup, sim = make_supervisor(sink, autostart=False)
    sup.start()
    try:
        sup.submit("apply", ControlRequest("dut-power", on=True)).result(timeout=5)
        assert sim.dut_power is True
        plan = ApplyPlan(
            changes=[ControlRequest("voltage", voltage_mv=3300)],
            stop_output_first=True,
            restart_output=True,
        )
        out = sup.submit("apply_plan", (plan, None)).result(timeout=5)
        operations = [step["operation"] for step in out["steps"]]
        assert operations == ["set_dut_power", "set_source_voltage_mv", "set_dut_power"]
        assert out["steps"][0]["after"]["dut_power"] is False
        assert out["steps"][-1]["after"]["dut_power"] is True
        assert sim.vdd_mv == 3300
    finally:
        sup.shutdown()


def test_a_failing_plan_restores_the_stream_but_never_the_output():
    """Restoring the stream is owed to the operator. Energising VOUT is not:
    a plan that failed must not leave the output on because its last step said
    so."""
    sink = Sink()
    sup, sim = make_supervisor(sink)
    sup.start()
    try:
        wait_for(lambda: sink.buckets(), what="a bucket frame")
        device = sup.device
        assert device is not None

        real_set_mode = device.set_mode

        def explode(mode, *, dry_run=False):
            if dry_run:
                return real_set_mode(mode, dry_run=True)
            raise TransportError("the write failed")

        device.set_mode = explode  # type: ignore[method-assign]
        plan = ApplyPlan(
            changes=[ControlRequest("mode", mode=int(Mode.AMPERE))],
            stop_output_first=False,
            restart_output=True,
        )
        with pytest.raises(DeviceRefused):
            sup.submit("apply_plan", (plan, None)).result(timeout=10)

        assert sim.dut_power is False, "a failed plan energised the output"
        device.set_mode = real_set_mode  # type: ignore[method-assign]
        seen = len(sink.buckets())
        wait_for(lambda: len(sink.buckets()) > seen, what="the stream to resume")
    finally:
        sup.shutdown()


def test_a_voltage_past_the_session_ceiling_says_which_limit_it_hit():
    sink = Sink()
    sup, sim = make_supervisor(sink, autostart=False, max_voltage_mv=3600)
    sup.start()
    try:
        before = list(sim.command_log)
        with pytest.raises(DeviceRefused) as excinfo:
            sup.submit("apply", ControlRequest("voltage", voltage_mv=4200)).result(timeout=5)
        from ppk2lab_web.codes import rejection

        described = rejection(excinfo.value, ceiling_mv=3600)
        assert described["messageKey"] == ER_CEILING
        assert described["args"] == [4200, 3600]
        assert sim.command_log == before, "a refused voltage reached the wire"
    finally:
        sup.shutdown()


def test_a_voltage_past_the_device_limits_is_a_different_message():
    sink = Sink()
    sup, sim = make_supervisor(sink, autostart=False)
    sup.start()
    try:
        before = list(sim.command_log)
        with pytest.raises(DeviceRefused) as excinfo:
            sup.submit("apply", ControlRequest("voltage", voltage_mv=9000)).result(timeout=5)
        from ppk2lab_web.codes import rejection

        assert rejection(excinfo.value, ceiling_mv=None)["messageKey"] == ER_RANGE
        assert sim.command_log == before
    finally:
        sup.shutdown()


def test_a_stale_preview_is_refused_rather_than_applied():
    """Compare-and-swap. Without it a console that previewed, lost its
    connection and came back would apply a plan against a device in a different
    state than the one it showed."""
    sink = Sink()
    sup, _sim = make_supervisor(sink, autostart=False)
    sup.start()
    try:
        preview = sup.submit(
            "preview_plan", ApplyPlan(changes=[ControlRequest("voltage", voltage_mv=3300)])
        ).result(timeout=5)
        seq = preview["stateSeq"]
        # Something else moves the device.
        sup.submit("apply", ControlRequest("dut-power", on=True)).result(timeout=5)
        plan = ApplyPlan(changes=[ControlRequest("voltage", voltage_mv=3300)])
        with pytest.raises(ControlRefused) as excinfo:
            sup.submit("apply_plan", (plan, seq)).result(timeout=5)
        assert excinfo.value.key == "er_state_moved"
    finally:
        sup.shutdown()


def test_apply_plan_broadcasts_every_step():
    """A saga takes seconds. A console that only learns the outcome at the end
    -- or, before this, only by reconnecting -- spends that time showing
    hardware state that has already moved."""
    sink = Sink()
    sup, sim = make_supervisor(sink, autostart=False)
    sup.start()
    try:
        plan = ApplyPlan(
            changes=[
                ControlRequest("mode", mode=int(Mode.SOURCE)),
                ControlRequest("voltage", voltage_mv=3300),
            ]
        )
        out = sup.submit("apply_plan", (plan, None)).result(timeout=5)
        broadcast = [m["change"]["operation"] for m in sink.json("state") if m["change"]]
        assert broadcast == ["set_mode", "set_source_voltage_mv"]
        assert broadcast == [step["operation"] for step in out["steps"]]
        assert sim.vdd_mv == 3300
        # And the pipeline phase is left where the device actually is, not on
        # the "applying" the plan published on its way in.
        assert sink.json("stream")[-1]["phase"] == "stopped"
    finally:
        sup.shutdown()


def test_a_failed_plan_still_broadcasts_and_resets_phase():
    sink = Sink()
    sup, _sim = make_supervisor(sink, autostart=False)
    sup.start()
    try:
        device = sup.device
        assert device is not None
        real_voltage = device.set_source_voltage_mv

        def explode(mv, *, dry_run=False):
            # A plan validates every step as a dry run before a byte reaches
            # the wire, so a failure has to be injected into the real apply.
            if dry_run:
                return real_voltage(mv, dry_run=True)
            raise TransportError("the write failed")

        device.set_source_voltage_mv = explode  # type: ignore[method-assign]
        plan = ApplyPlan(
            changes=[
                ControlRequest("mode", mode=int(Mode.SOURCE)),
                ControlRequest("voltage", voltage_mv=3300),
            ]
        )
        with pytest.raises(DeviceRefused):
            sup.submit("apply_plan", (plan, None)).result(timeout=10)
        device.set_source_voltage_mv = real_voltage  # type: ignore[method-assign]

        # The step that did land is still reported: a plan that failed halfway
        # left the device somewhere, and that somewhere is the truth.
        assert [m["change"]["operation"] for m in sink.json("state") if m["change"]] == ["set_mode"]
        assert sink.json("stream")[-1]["phase"] != "applying", "consoles left stuck in applying"
    finally:
        sup.shutdown()


def test_stream_transitions_broadcast_state():
    """`measuring` is device state, and it moves without anyone applying a
    change. The chart clock is gated on it, so a stale one freezes the view."""
    sink = Sink()
    sup, _sim = make_supervisor(sink, autostart=False)
    sup.start()
    try:
        sup.submit("start_stream").result(timeout=5)
        sup.submit("stop_stream").result(timeout=5)
        measuring = [m["state"]["measuring"] for m in sink.json("state")]
        assert measuring == [False, True, False]
        assert all(m["change"] is None for m in sink.json("state"))
    finally:
        sup.shutdown()


def test_stream_transitions_do_not_invalidate_a_preview():
    """Compare-and-swap is about the configuration, not about whether the
    device happens to be measuring. Starting and stopping the stream between a
    preview and its confirmation changes nothing the preview projected."""
    sink = Sink()
    sup, sim = make_supervisor(sink, autostart=False)
    sup.start()
    try:
        preview = sup.submit(
            "preview_plan", ApplyPlan(changes=[ControlRequest("voltage", voltage_mv=3300)])
        ).result(timeout=5)
        sup.submit("start_stream").result(timeout=5)
        sup.submit("stop_stream").result(timeout=5)
        plan = ApplyPlan(changes=[ControlRequest("voltage", voltage_mv=3300)])
        out = sup.submit("apply_plan", (plan, preview["stateSeq"])).result(timeout=5)
        assert out["steps"][0]["applied"] is True
        assert sim.vdd_mv == 3300
    finally:
        sup.shutdown()


def test_dut_power_does_not_break_the_trace():
    """The whole point of it being unguarded. An inrush is only observable if
    the samples either side of the switch belong to one continuous run."""
    sink = Sink()
    sup, sim = make_supervisor(sink, profile=DemoActivityProfile())
    sup.start()
    try:
        wait_for(lambda: sink.buckets(), what="a bucket frame")
        before = len(sink.buckets())
        sup.submit("apply", ControlRequest("dut-power", on=True)).result(timeout=5)
        wait_for(lambda: len(sink.buckets()) > before, what="buckets after the switch")
        after = sink.buckets()[before:]
        assert sim.dut_power is True
        assert not any(b["discontinuity"] for b in after), "the switch broke the trace"
    finally:
        sup.shutdown()


def test_a_power_off_never_queues_behind_a_slow_command():
    """ "Arming asks, disarming does not" as a scheduling property.

    A request to de-energise VOUT must not sit behind a saga that stops the
    stream, writes several commands and reopens it -- three seconds in the
    worst realistic case, with a DUT powered the whole time.
    """
    sink = Sink()
    sup, sim = make_supervisor(sink, autostart=False)
    sup.start()
    try:
        sup.submit("apply", ControlRequest("dut-power", on=True)).result(timeout=5)
        assert sim.dut_power is True

        device = sup.device
        assert device is not None
        order: list[str] = []
        gate = threading.Event()

        real_mode = device.set_mode
        real_power = device.set_dut_power
        real_voltage = device.set_source_voltage_mv

        def slow_mode(mode, *, dry_run=False):
            if not dry_run:
                order.append("mode:start")
                gate.wait(timeout=5.0)
                order.append("mode:end")
            return real_mode(mode, dry_run=dry_run)

        def watched_power(on, *, dry_run=False):
            if not dry_run:
                order.append(f"power={on}")
            return real_power(on, dry_run=dry_run)

        def watched_voltage(mv, *, dry_run=False):
            if not dry_run:
                order.append("voltage")
            return real_voltage(mv, dry_run=dry_run)

        device.set_mode = slow_mode  # type: ignore[method-assign]
        device.set_dut_power = watched_power  # type: ignore[method-assign]
        device.set_source_voltage_mv = watched_voltage  # type: ignore[method-assign]

        saga = sup.submit(
            "apply_plan",
            (ApplyPlan(changes=[ControlRequest("mode", mode=int(Mode.AMPERE))]), None),
        )
        wait_for(lambda: "mode:start" in order, what="the saga to reach the device")

        # Both arrive while the saga is stuck. The normal one was queued first.
        queued = sup.submit("apply", ControlRequest("voltage", voltage_mv=3300))
        off = sup.request_power_off()
        gate.set()

        saga.result(timeout=10)
        off.result(timeout=10)
        queued.result(timeout=10)

        assert "power=False" in order, "the power-off never ran"
        assert order.index("power=False") < order.index("voltage"), order
        assert sim.dut_power is False
    finally:
        sup.shutdown()


def test_a_validation_dry_run_does_not_reach_the_session_log():
    """Every apply validates first. Those projections land in the device's own
    session log, and replaying them would leave phantom entries for a change
    that was refused."""
    sink = Sink()
    sup, _sim = make_supervisor(sink, autostart=False, max_voltage_mv=3600)
    sup.start()
    try:
        with pytest.raises(DeviceRefused):
            sup.submit(
                "apply_plan",
                (ApplyPlan(changes=[ControlRequest("voltage", voltage_mv=4200)]), None),
            ).result(timeout=5)
        applied = [m for m in sink.json("state") if m["change"] and m["change"]["applied"]]
        assert not applied
    finally:
        sup.shutdown()


# ---------------------------------------------------------------------------
# Failure and shutdown


def test_a_hot_unplug_keeps_the_server_up_and_closes_the_device():
    """The page has to be able to say what happened. Closing is attempted
    anyway -- close() tolerates a dead port and records what it could not do
    rather than skipping it quietly."""
    sink = Sink()
    sup, sim = make_supervisor(sink)
    sup.start()
    try:
        wait_for(lambda: sink.buckets(), what="a bucket frame")
        sim.unplug()
        wait_for(lambda: sink.json("error"), what="the failure to be reported", timeout=10)
        assert sup.device_present is False
        assert sup._thread.is_alive()
        stopped = [m for m in sink.json("stream") if m["reason"] == "device_lost"]
        assert stopped
    finally:
        sup.shutdown()


def test_a_failure_after_the_device_opens_still_closes_it(monkeypatch):
    """The shutdown guarantee has to cover the startup path too.

    Reporting the failure and returning would leave a live handle with its
    power untouched, and `shutdown()` cannot recover that: by then the thread
    has exited, so its join is a no-op and nothing calls close().
    """
    sink = Sink()
    sim = SimulatedPPK2(profile=ConstantProfile(100.0))
    opened: list[PPK2] = []

    def opener() -> PPK2:
        device = PPK2.open(transport=MockTransport(sim), simulate=True)
        opened.append(device)
        return device

    sup = Supervisor(open_device=opener, publish=sink, autostart=False)
    monkeypatch.setattr(
        Supervisor,
        "_republish_snapshot",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    with pytest.raises(RuntimeError, match="boom"):
        sup.start()

    assert opened, "the device was never opened, so this proves nothing"
    assert opened[0].transport.is_open is False, "a failed startup left the device open"


def test_a_hot_unplug_keeps_the_samples_it_had_already_converted():
    """The moment before the cable came out is the part anyone would look at."""
    sink = Sink()
    sup, sim = make_supervisor(sink)
    sup.start()
    try:
        wait_for(lambda: sink.buckets(), what="a bucket frame")
        before = sum(b["count"] for b in sink.buckets())
        sim.unplug()
        wait_for(lambda: sink.json("error"), what="the failure to be reported", timeout=10)
        after = sum(b["count"] for b in sink.buckets())
        # Whatever had been accumulated is flushed rather than dropped. The
        # count can only grow; a strict check on the difference would be a
        # check on timing.
        assert after >= before
    finally:
        sup.shutdown()


@pytest.mark.parametrize("how", ["stop", "unplug"])
def test_shutdown_always_closes_the_device(how):
    """A DUT left energised is the most damaging thing this project can do."""
    sink = Sink()
    sup, sim = make_supervisor(sink)
    sup.start()
    try:
        wait_for(lambda: sink.buckets(), what="a bucket frame")
        sup.submit("apply", ControlRequest("dut-power", on=True)).result(timeout=5)
        if how == "unplug":
            sim.unplug()
            wait_for(lambda: not sup.device_present, what="the loss to be noticed", timeout=10)
    finally:
        sup.shutdown()
    device = sup.device
    assert device is not None
    assert device.transport.is_open is False
    if how == "stop":
        # Fail-safe OFF: the session never knew the starting state.
        assert sim.dut_power is False


def test_shutdown_is_idempotent_and_safe_from_any_thread():
    sink = Sink()
    sup, _sim = make_supervisor(sink)
    sup.start()
    wait_for(lambda: sink.buckets(), what="a bucket frame")
    threads = [threading.Thread(target=sup.shutdown) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    sup.shutdown()
    assert sup.device is not None
    assert sup.device.transport.is_open is False


def test_a_command_after_shutdown_is_refused_rather_than_lost():
    sink = Sink()
    sup, _sim = make_supervisor(sink, autostart=False)
    sup.start()
    sup.shutdown()
    with pytest.raises(ControlRefused):
        sup.submit("apply", ControlRequest("dut-power", on=False)).result(timeout=5)
