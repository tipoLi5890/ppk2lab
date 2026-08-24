"""The ASGI layer: what is reachable, by whom, and what a slow client is told.

Everything here runs in-process through Starlette's test client -- no socket,
no port, no flake. The device underneath is the simulator.
"""

from __future__ import annotations

import asyncio
import time
from array import array
from typing import Any

import pytest
from starlette.testclient import TestClient

from ppk2lab.device import PPK2
from ppk2lab.testing.profiles import ConstantProfile, DemoActivityProfile
from ppk2lab.transport.mock import MockTransport, SimulatedPPK2
from ppk2lab.types import SAMPLE_RATE_HZ
from ppk2lab_web import protocol, static_dir
from ppk2lab_web.app import create_app
from ppk2lab_web.buckets import Tier0Accumulator
from ppk2lab_web.hub import DATA_QUEUE, Connection, Hub
from ppk2lab_web.supervisor import HISTOGRAM_INTERVAL_S, Supervisor

ORIGIN = "http://test.local"


class Harness:
    def __init__(self, supervisor: Supervisor, app: Any, simulator: SimulatedPPK2) -> None:
        self.supervisor = supervisor
        self.app = app
        self.simulator = simulator


@pytest.fixture
def harness(request):
    allow_control = getattr(request, "param", {}).get("allow_control", True)
    profile = getattr(request, "param", {}).get("profile") or ConstantProfile(100.0)
    autostart = getattr(request, "param", {}).get("autostart", True)
    rate = getattr(request, "param", {}).get("rate_limit_hz")
    sim = SimulatedPPK2(profile=profile, rate_limit_hz=rate)
    supervisor = Supervisor(
        open_device=lambda: PPK2.open(transport=MockTransport(sim), simulate=True),
        publish=lambda message, target: None,
        allow_control=allow_control,
        autostart=autostart,
    )
    app = create_app(supervisor, static_dir=static_dir(), allowed_origins={ORIGIN})
    supervisor.set_publisher(app.state.console.hub.publish)
    supervisor.start()
    try:
        yield Harness(supervisor, app, sim)
    finally:
        supervisor.shutdown()


def _open(harness: Harness, client: TestClient):
    return client.websocket_connect("/ws", headers={"Origin": ORIGIN})


def _reply(ws, op: str, **fields) -> dict[str, Any]:
    ws.send_json({"id": "1", "op": op, **fields})
    while True:
        message = ws.receive()
        if "text" not in message or message["text"] is None:
            continue
        import json

        payload = json.loads(message["text"])
        if payload.get("type") == "result" and payload.get("id") == "1":
            return payload


def _next_json_of(ws, kind: str, *, budget: int = 200) -> dict[str, Any]:
    """The first message of one type, skipping the binary flood around it.

    Separate from `_reply` rather than folded into it: `_reply` deliberately
    discards what it passes, and a broadcast test is exactly the case where
    those discarded messages are the evidence.
    """
    import json

    for _ in range(budget):
        message = ws.receive()
        text = message.get("text")
        if not text:
            continue
        payload = json.loads(text)
        if payload.get("type") == kind:
            return payload
    raise AssertionError(f"no {kind!r} message in {budget} messages")


# ---------------------------------------------------------------------------
# What is served


def test_it_serves_the_console_and_a_read_only_snapshot(harness):
    with TestClient(harness.app) as client:
        page = client.get("/", headers={"Origin": ORIGIN})
        assert page.status_code == 200
        assert b"app.js" in page.content

        snapshot = client.get("/api/session", headers={"Origin": ORIGIN}).json()
        assert snapshot["protocol"] == protocol.PROTOCOL_VERSION
        assert snapshot["device"]["simulated"] is True
        assert snapshot["device_present"] is True


def test_a_socket_starts_with_hello_and_then_samples(harness):
    with TestClient(harness.app) as client, _open(harness, client) as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello"
        assert hello["protocol"] == protocol.PROTOCOL_VERSION
        # The version gate: a console that does not speak it must stop here
        # rather than decode a binary frame whose layout it is guessing at.
        assert hello["session"]["bucket_us"] == 1000
        assert hello["session"]["control_token"]
        assert hello["state"]["measuring"] is True

        for _ in range(80):
            message = ws.receive()
            frame = message.get("bytes")
            # Two kinds of binary frame share this socket and neither is
            # promised first: the histogram is published on its own timer, so
            # on a loaded machine it can beat the first bucket batch out. Match
            # on the tag rather than taking whatever arrives, which is what
            # `test_a_console_that_attaches_late_is_sent_the_histogram` below
            # already does.
            if frame and frame[0] == protocol.TAG_BUCKETS:
                batch = protocol.unpack_buckets(frame)
                assert batch["count"] >= 1
                return
        pytest.fail("no bucket frame arrived")


# ---------------------------------------------------------------------------
# Origin and token


@pytest.mark.parametrize("origin", ["http://evil.example", None])
def test_a_socket_from_anywhere_else_is_refused(harness, origin):
    """A WebSocket is not subject to the same-origin policy: any page can open
    one and the browser will not stop it. A local process opening a raw socket
    sends no Origin at all. Neither gets one here, because this is where the
    control token is handed out."""
    headers = {"Origin": origin} if origin else {}
    with TestClient(harness.app) as client:
        with pytest.raises(Exception):  # noqa: B017 - starlette raises on a 1008 close
            with client.websocket_connect("/ws", headers=headers) as ws:
                ws.receive_json()


def test_a_foreign_page_cannot_read_the_snapshot_but_a_script_can(harness):
    """The threat is a page on another site, and a page always sends an Origin.

    A caller with none is a local script, which gains nothing here it could not
    get from `ppk2lab info` when the server is not holding the port -- and
    refusing it would leave no way at all to read device state while it is.
    """
    with TestClient(harness.app) as client:
        assert (
            client.get("/api/session", headers={"Origin": "http://evil.example"}).status_code == 403
        )
        assert client.get("/api/session").status_code == 200


def test_a_command_without_this_connection_s_token_is_refused(harness):
    """Not a lease problem: something that never received this connection's
    hello is trying to command the device."""
    with TestClient(harness.app) as client, _open(harness, client) as ws:
        ws.receive_json()
        reply = _reply(ws, "apply", request={"kind": "dut-power", "on": True}, token="guessed")
        assert reply["ok"] is False
        assert reply["error"]["messageKey"] == "er_locked"
        assert harness.simulator.dut_power is False


# ---------------------------------------------------------------------------
# The control gate


@pytest.mark.parametrize("harness", [{"allow_control": False}], indirect=True)
def test_without_allow_control_nothing_reaches_the_device(harness):
    """The gate is structural. A device double that fails the test if it is
    ever asked to change state proves there is no path, rather than that this
    particular request happened to be refused."""
    device = harness.supervisor.device
    assert device is not None

    def forbidden(*args, **kwargs):
        if not kwargs.get("dry_run"):
            raise AssertionError("a locked server reached the device")
        raise AssertionError("locked servers must not even validate")

    device.set_dut_power = forbidden  # type: ignore[method-assign]
    device.set_mode = forbidden  # type: ignore[method-assign]
    device.set_source_voltage_mv = forbidden  # type: ignore[method-assign]

    with TestClient(harness.app) as client, _open(harness, client) as ws:
        hello = ws.receive_json()
        assert hello["session"]["control_allowed"] is False
        token = hello["session"]["control_token"]
        for op, fields in [
            ("apply", {"request": {"kind": "dut-power", "on": True}}),
            ("apply_plan", {"plan": {"changes": [{"kind": "voltage", "voltageMv": 3300}]}}),
            ("start_stream", {}),
            ("stop_stream", {}),
            ("record", {"options": {"durationS": 0.01}}),
        ]:
            reply = _reply(ws, op, token=token, **fields)
            assert reply["ok"] is False, op
            assert reply["error"]["messageKey"] == "er_locked", op


@pytest.mark.parametrize("harness", [{"allow_control": False, "autostart": False}], indirect=True)
def test_a_dry_run_is_available_even_on_a_locked_server(harness):
    """It sends no bytes. Refusing it would only stop an operator seeing what
    a change would do before deciding whether to restart with control on."""
    with TestClient(harness.app) as client, _open(harness, client) as ws:
        hello = ws.receive_json()
        reply = _reply(
            ws,
            "preview",
            request={"kind": "voltage", "voltageMv": 3300},
            token=hello["session"]["control_token"],
        )
        assert reply["ok"] is True
        assert reply["result"]["applied"] is False
        assert harness.simulator.vdd_mv == 3000


def test_control_works_when_it_is_enabled(harness):
    with TestClient(harness.app) as client, _open(harness, client) as ws:
        hello = ws.receive_json()
        token = hello["session"]["control_token"]
        reply = _reply(ws, "apply", request={"kind": "dut-power", "on": True}, token=token)
        assert reply["ok"] is True
        assert reply["result"]["applied"] is True
        # Never confirmed: DUT power cannot be read back from the device.
        assert reply["result"]["observed_after"] is False
        assert harness.simulator.dut_power is True


def test_a_refused_voltage_names_the_limit_it_hit(harness):
    with TestClient(harness.app) as client, _open(harness, client) as ws:
        hello = ws.receive_json()
        reply = _reply(
            ws,
            "apply",
            request={"kind": "voltage", "voltageMv": 9000},
            token=hello["session"]["control_token"],
        )
        assert reply["ok"] is False
        assert reply["error"]["messageKey"] == "er_range"
        assert reply["error"]["detail"]["code"] == "VOLTAGE_OUT_OF_RANGE"


def test_a_second_console_can_watch_but_not_command(harness):
    """Every viewer sees everything -- an instrument shows the same reading to
    everyone in the room -- but only one connection may command it."""
    with TestClient(harness.app) as client, _open(harness, client) as first:
        hello_one = first.receive_json()
        reply = _reply(
            first,
            "apply",
            request={"kind": "dut-power", "on": True},
            token=hello_one["session"]["control_token"],
        )
        assert reply["ok"] is True

        with _open(harness, client) as second:
            hello_two = second.receive_json()
            assert hello_two["type"] == "hello"  # it can watch
            reply = _reply(
                second,
                "apply",
                request={"kind": "dut-power", "on": False},
                token=hello_two["session"]["control_token"],
            )
            assert reply["ok"] is False
            assert reply["error"]["messageKey"] == "er_held"


def test_a_second_console_sees_the_change(harness):
    """The other half of "every viewer sees everything": a console that did not
    command the device still has to be told the device moved, without having to
    reconnect to find out."""
    with TestClient(harness.app) as client, _open(harness, client) as ws_a:
        hello_a = ws_a.receive_json()
        with _open(harness, client) as ws_b:
            ws_b.receive_json()
            reply = _reply(
                ws_a,
                "apply",
                request={"kind": "dut-power", "on": True},
                token=hello_a["session"]["control_token"],
            )
            assert reply["ok"] is True
            state = _next_json_of(ws_b, "state")
            assert state["state"]["dut_power"] is True
            assert state["change"]["operation"] == "set_dut_power"


def test_a_recording_must_be_bounded(harness):
    """`run_capture` has no cooperative stop, so an unbounded recording would
    hold the device until the process ended."""
    with TestClient(harness.app) as client, _open(harness, client) as ws:
        hello = ws.receive_json()
        reply = _reply(ws, "record", options={}, token=hello["session"]["control_token"])
        assert reply["ok"] is False
        assert "duration" in reply["error"]["args"][1]


def test_a_recording_writes_a_real_artifact_and_keeps_the_console_drawing(
    harness, tmp_path, monkeypatch
):
    """The console records through the library, not around it.

    `run_capture` opens its own stream, so the live one comes down first -- and
    the live view is then fed from the capture's own blocks through `on_block`,
    which is what makes the trace and the artifact incapable of disagreeing.
    """
    monkeypatch.chdir(tmp_path)
    with TestClient(harness.app) as client, _open(harness, client) as ws:
        hello = ws.receive_json()
        reply = _reply(
            ws,
            "record",
            options={"durationS": 0.05, "output": "run.ppk2a"},
            token=hello["session"]["control_token"],
        )
        assert reply["ok"] is True, reply
        result = reply["result"]
        assert result["complete"] is True
        assert result["stored_samples"] == 5000
        assert result["capture_sha256"]
        written = tmp_path / "run.ppk2a"
        assert written.is_file()
        assert written.stat().st_size > 0

    # The device is measuring again afterwards, because it was before.
    assert harness.supervisor.device is not None


@pytest.mark.parametrize("harness", [{"rate_limit_hz": float(SAMPLE_RATE_HZ)}], indirect=True)
def test_a_power_off_is_serviced_while_a_recording_holds_the_device(harness, tmp_path, monkeypatch):
    """The fail-safe direction cannot wait for a recording to finish.

    A recording is bounded only by the duration its operator chose and cannot
    be cancelled, so an operator who reaches for the power-off button because
    something is going wrong must not be told to wait it out. Two things had to
    be true and neither was: the socket has to keep reading while a command is
    in flight, and a power-off has to travel the queue lane the supervisor
    services from inside the capture.

    The assertion is the *order* of the replies, which is the only evidence
    that the power-off was serviced during the recording rather than after it.
    """
    monkeypatch.chdir(tmp_path)
    with TestClient(harness.app) as client, _open(harness, client) as ws:
        hello = ws.receive_json()
        token = hello["session"]["control_token"]

        ws.send_json(
            {
                "id": "rec",
                "op": "record",
                "token": token,
                "options": {"durationS": 0.6, "output": "held.ppk2a"},
            }
        )
        ws.send_json(
            {
                "id": "off",
                "op": "apply",
                "token": token,
                "request": {"kind": "dut-power", "on": False},
            }
        )

        order: list[str] = []
        import json as _json

        while len(order) < 2:
            message = ws.receive()
            text = message.get("text")
            if not text:
                continue
            payload = _json.loads(text)
            if payload.get("type") == "result":
                order.append(payload["id"])
                assert payload["ok"] is True, payload

        assert order == ["off", "rec"], (
            "the power-off was not serviced until the recording finished: " + str(order)
        )


def test_a_recording_cannot_choose_where_to_write(harness):
    """A browser must not be able to name a path outside the server's own
    directory."""
    with TestClient(harness.app) as client, _open(harness, client) as ws:
        hello = ws.receive_json()
        reply = _reply(
            ws,
            "record",
            options={"durationS": 0.01, "output": "../../etc/passwd.ppk2a"},
            token=hello["session"]["control_token"],
        )
        assert reply["ok"] is False
        assert "file name" in reply["error"]["args"][1]


# ---------------------------------------------------------------------------
# Lifecycle


def test_the_lifespan_closes_the_device(harness):
    with TestClient(harness.app) as client:
        client.get("/api/session", headers={"Origin": ORIGIN})
    assert harness.simulator.measuring is False
    device = harness.supervisor.device
    assert device is not None
    assert device.transport.is_open is False


# ---------------------------------------------------------------------------
# Back-pressure, as a unit


def _frame(start: int, count: int) -> bytes:
    acc = Tier0Accumulator(start_index=start)
    buckets = acc.add_samples([1.0] * (100 * count), bytes(100 * count), bytes(100 * count))
    return protocol.pack_buckets(buckets)


def test_a_slow_console_is_told_exactly_what_it_missed():
    """Not merely that it fell behind. The console breaks its trace over that
    stretch and counts it apart from instrument loss, because "your browser did
    not receive it" and "the instrument never delivered it" are different
    facts."""

    async def scenario():
        connection = Connection("c1")
        for i in range(DATA_QUEUE + 5):
            connection.offer_data(_frame(i * 100, 1))
        note = connection.take_desync()
        assert note is not None
        assert note["type"] == "desync"
        assert note["from_index"] == 0
        assert note["buckets_dropped"] == DATA_QUEUE
        # Taken once: a second call has nothing left to report.
        assert connection.take_desync() is None
        return True

    assert asyncio.run(scenario())


def test_the_distribution_survives_a_discard():
    """Dropping it buys nothing and costs a whole panel.

    Sample frames are a stretch of timeline: dropping one is reportable, the
    console draws it as a display gap, and the next frame carries the next
    stretch. The distribution grid is absolute state -- there is no stretch to
    report, and its "the next one heals it" argument is only true if a next one
    arrives. Under sustained back-pressure none did: every periodic grid went
    into a discard, and the panel sat frozen at whatever it held when the
    console attached, with nothing on screen to say so.

    Measured on the simulator at full tilt: one grid per 120 messages before,
    which was the attach frame and nothing after it; three or four after, which
    is the 1 Hz cadence over the same four seconds.
    """

    async def scenario():
        connection = Connection("c1")
        grid = protocol.pack_histogram(array("d", [7.0] * 170))
        connection.offer_data(grid)
        for i in range(DATA_QUEUE + 5):
            connection.offer_data(_frame(i * 100, 1))

        frames = []
        while not connection.data.empty():
            frames.append(connection.data.get_nowait())
        kept = [f for f in frames if f[0] == protocol.TAG_HISTOGRAM]
        assert len(kept) == 1, "the newest grid, and only it, should survive"
        assert protocol.unpack_histogram(kept[0])[0] == 7.0

        # And the sample frames it did drop are still reported: keeping the
        # grid must not quietly absorb a real gap. The queue held the grid plus
        # `DATA_QUEUE - 1` sample frames, and the grid is not one of them.
        note = connection.take_desync()
        assert note is not None
        assert note["buckets_dropped"] == DATA_QUEUE - 1
        assert note["from_index"] == 0
        return True

    assert asyncio.run(scenario())


def test_control_messages_are_never_dropped():
    """A console that silently missed a state change would show hardware state
    that stopped being true."""

    async def scenario():
        connection = Connection("c1")
        for i in range(256):
            assert connection.offer_control({"type": "state", "n": i}) is True
        # Past capacity it is closed rather than quietly falling behind.
        assert connection.offer_control({"type": "state", "n": 999}) is False
        return True

    assert asyncio.run(scenario())


def test_the_lease_is_released_when_a_console_goes_away():
    """Anything else would need a timeout to recover from a closed laptop lid."""

    async def scenario():
        hub = Hub()
        first = hub.attach()
        second = hub.attach()
        assert hub.claim_control(first) is True
        assert hub.claim_control(second) is False
        hub.detach(first)
        assert hub.lease_holder is None
        assert hub.claim_control(second) is True
        return True

    assert asyncio.run(scenario())


def test_publishing_before_a_loop_exists_is_a_no_op():
    """The supervisor starts before the ASGI lifespan runs, so it can publish
    into a hub that has no loop yet. That must not raise on the device thread."""
    hub = Hub()
    hub.publish({"type": "state"})  # must not raise
    assert len(hub) == 0


@pytest.mark.parametrize("harness", [{"profile": DemoActivityProfile()}], indirect=True)
def test_a_console_that_attaches_late_is_sent_the_histogram(harness):
    """Its decimator starts empty, and a per-sample distribution cannot be
    rebuilt from buckets."""
    with TestClient(harness.app) as client, _open(harness, client) as ws:
        ws.receive_json()
        # Budgeted by the cadence rather than by a message count. The mix on
        # this socket is not the test's to control -- the simulator here runs
        # unthrottled, so a hundred sample frames can pass in the time one grid
        # is due -- and counting messages made this depend on the attach frame
        # in particular, which is one frame among a flood a slow reader may
        # discard. Three intervals gives the periodic grid several turns.
        deadline = time.monotonic() + 3 * HISTOGRAM_INTERVAL_S
        seen = 0
        while time.monotonic() < deadline and seen < 600:
            message = ws.receive()
            seen += 1
            frame = message.get("bytes")
            if frame and frame[0] == protocol.TAG_HISTOGRAM:
                bins = protocol.unpack_histogram(frame)
                assert len(bins) == 169
                return
        pytest.fail(f"no histogram frame in {seen} messages")
