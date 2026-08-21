"""The ASGI application: static console, one WebSocket, no state-changing HTTP.

Every mutation rides the WebSocket. That is not a stylistic preference, it is
what makes the CSRF question go away: a WebSocket is *not* subject to the
same-origin policy -- any page can open ``ws://127.0.0.1:…`` and the browser
will send an ``Origin`` header without enforcing anything -- so the handshake
has to check the origin and a per-connection token regardless. Once that gate
exists, putting the commands behind it leaves no state-changing HTTP endpoint
for a cross-origin form to target. All HTTP here is GET.

Loopback is not an authorization boundary either: any local process can
connect, and it arrives with no ``Origin`` at all. The token is what separates
"the console this server handed out" from "something else on this machine".
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from . import protocol
from .codes import ER_HELD, ER_LOCKED, ControlRefused, rejection
from .hub import Hub
from .supervisor import ApplyPlan, ControlRequest

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterable

    from .supervisor import Supervisor

#: Client operations that reach the device. Everything else is read-only.
CONTROL_OPS = frozenset({"apply", "apply_plan", "start_stream", "stop_stream", "record"})

#: Operations allowed with no lease and no control flag. A dry run writes
#: nothing, so gating it would only stop an operator seeing what a change would
#: do before deciding.
READ_OPS = frozenset({"ping", "preview", "preview_plan", "replay", "claim_control", "release"})


class Console:
    """Wires a supervisor, a hub and an ASGI app together."""

    def __init__(
        self,
        supervisor: Supervisor,
        *,
        static_dir: Any,
        allowed_origins: Iterable[str] = (),
        require_token: bool = True,
    ) -> None:
        self.supervisor = supervisor
        self.hub = Hub()
        self.require_token = require_token
        self.allowed_origins = {o.rstrip("/") for o in allowed_origins}
        self.app = Starlette(
            routes=[
                WebSocketRoute("/ws", self._websocket),
                Route("/api/session", self._session, methods=["GET"]),
                # Mounted last: it serves "/" and would otherwise shadow the
                # routes above.
                self._static(static_dir),
            ],
            lifespan=self._lifespan,
        )

    def _static(self, static_dir: Any) -> Any:
        from starlette.routing import Mount

        return Mount("/", app=StaticFiles(directory=str(static_dir), html=True), name="console")

    @contextlib.asynccontextmanager
    async def _lifespan(self, _app: Starlette):
        self.hub.bind(asyncio.get_running_loop())
        try:
            yield
        finally:
            # One of three independent guarantees that the device is closed
            # with its power restored. The others are the supervisor's own
            # `finally` and the atexit hook in server.py.
            await asyncio.to_thread(self.supervisor.shutdown)

    # -- HTTP -------------------------------------------------------------
    async def _session(self, request: Any) -> JSONResponse:
        """A read-only snapshot, for a page that has not opened a socket yet
        and for anything scripting the server."""
        if not self._origin_ok(request.headers.get("origin"), allow_absent=True):
            return JSONResponse({"error": "origin not allowed"}, status_code=403)
        snapshot = dict(self.supervisor.snapshot)
        snapshot["protocol"] = protocol.PROTOCOL_VERSION
        snapshot["control_allowed"] = self.supervisor.allow_control
        snapshot["device_present"] = self.supervisor.device_present
        return JSONResponse(snapshot)

    def _origin_ok(self, origin: str | None, *, allow_absent: bool = False) -> bool:
        """Is this request from a page this server handed out?

        A *foreign* origin is always refused: the threat is a page on another
        site opening a socket to this one, and a page always sends an origin.

        An *absent* origin is a different caller -- a local process with no
        browser involved -- and the two are separated deliberately. The
        WebSocket refuses it, because that is where the control token is handed
        out and where every state change goes. The read-only snapshot admits
        it, because a script gains nothing there it could not get by running
        `ppk2lab info` when the server is not holding the port, and refusing it
        would leave no way at all to read device state while it is.
        """
        if origin is None:
            return allow_absent or not self.require_token
        return origin.rstrip("/") in self.allowed_origins

    # -- WebSocket --------------------------------------------------------
    async def _websocket(self, ws: WebSocket) -> None:
        if not self._origin_ok(ws.headers.get("origin")):
            # 1008 policy violation, refused before the handshake completes so
            # nothing is ever sent to a page that should not have it.
            await ws.close(code=1008)
            return
        await ws.accept()
        connection = self.hub.attach()
        supervisor = self.supervisor
        try:
            await ws.send_json(self._hello(connection))
            supervisor.submit("replay", origin=connection.id)
            writer = asyncio.ensure_future(self._writer(ws, connection))
            try:
                await self._reader(ws, connection)
            finally:
                connection.closed.set()
                writer.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await writer
        except WebSocketDisconnect:
            pass
        finally:
            self.hub.detach(connection)

    def _hello(self, connection: Any) -> dict[str, Any]:
        snapshot = self.supervisor.snapshot
        return protocol.hello(
            session_id=self.supervisor.session_id,
            control_allowed=self.supervisor.allow_control,
            control_token=connection.control_token,
            attach_index=snapshot.get("attach_index", 0),
            history_seconds=self.supervisor.history_seconds,
            device=snapshot.get("device", {"simulated": False}),
            state=snapshot.get("state", {}),
            calibration=snapshot.get("calibration", {}),
            max_voltage_mv=snapshot.get("max_voltage_mv"),
            counters=snapshot.get("counters", {}),
            streaming=bool(snapshot.get("streaming")),
            state_seq=self.supervisor.state_seq,
            warnings=snapshot.get("warnings", []),
        )

    async def _writer(self, ws: WebSocket, connection: Any) -> None:
        """Drain both queues, control first.

        The two `get()` tasks are kept alive across iterations and only the one
        that completed is replaced. Cancelling a pending `Queue.get()` between
        rounds would be the kind of thing that loses one message in a thousand
        and is never reproduced.
        """
        ctrl = asyncio.ensure_future(connection.ctrl.get())
        data = asyncio.ensure_future(connection.data.get())
        try:
            while True:
                done, _ = await asyncio.wait(
                    {ctrl, data}, return_when=asyncio.FIRST_COMPLETED, timeout=0.5
                )
                if connection.closed.is_set():
                    return
                if ctrl in done:
                    await ws.send_json(ctrl.result())
                    ctrl = asyncio.ensure_future(connection.ctrl.get())
                    continue
                if data in done:
                    # A connection that fell behind is told what it missed
                    # before it is sent anything newer, so the console can break
                    # its trace at the right place.
                    note = connection.take_desync()
                    if note is not None:
                        await ws.send_json(note)
                    await ws.send_bytes(data.result())
                    data = asyncio.ensure_future(connection.data.get())
        finally:
            for task in (ctrl, data):
                task.cancel()

    async def _reader(self, ws: WebSocket, connection: Any) -> None:
        while True:
            try:
                raw = await ws.receive_json()
            except WebSocketDisconnect:
                return
            except Exception:  # a malformed frame ends the socket
                return
            if not isinstance(raw, dict):
                continue
            await self._handle(ws, connection, raw)

    async def _handle(self, ws: WebSocket, connection: Any, raw: dict[str, Any]) -> None:
        op = raw.get("op")
        request_id = str(raw.get("id", ""))
        if op == "ping":
            await ws.send_json({"type": "pong", "id": request_id})
            return
        try:
            payload = self._authorise_and_build(connection, op, raw)
        except ControlRefused as exc:
            await ws.send_json(
                protocol.result(request_id=request_id, ok=False, error=rejection(exc))
            )
            return

        if op == "claim_control":
            await ws.send_json(protocol.result(request_id=request_id, ok=True, held=True))
            return
        if op == "release":
            self.hub.release_control(connection)
            await ws.send_json(protocol.result(request_id=request_id, ok=True, held=False))
            return

        future = self.supervisor.submit(str(op), payload, origin=connection.id)
        try:
            value = await asyncio.wrap_future(future)
        except BaseException as exc:  # reported to the caller, never swallowed
            await ws.send_json(
                protocol.result(
                    request_id=request_id,
                    ok=False,
                    error=rejection(exc, ceiling_mv=self.supervisor.max_voltage_mv),
                )
            )
            return
        await ws.send_json(protocol.result(request_id=request_id, ok=True, result=value))

    def _authorise_and_build(self, connection: Any, op: Any, raw: dict[str, Any]) -> Any:
        if op not in CONTROL_OPS and op not in READ_OPS:
            raise ControlRefused("er_unknown", ["BAD_REQUEST", f"unknown op {op!r}"])

        if op in CONTROL_OPS or op == "claim_control":
            if not self.supervisor.allow_control:
                raise ControlRefused(ER_LOCKED)
            if self.require_token and raw.get("token") != connection.control_token:
                # Not a lease problem: something that did not receive this
                # connection's hello is trying to command the device.
                raise ControlRefused(ER_LOCKED)
            if not self.hub.claim_control(connection):
                raise ControlRefused(ER_HELD, [str(self.hub.lease_holder)])

        if op in ("apply", "preview"):
            return ControlRequest.from_json(raw.get("request") or {})
        if op in ("apply_plan", "preview_plan"):
            plan = ApplyPlan.from_json(raw.get("plan") or {})
            if op == "apply_plan":
                return (plan, raw.get("stateSeq"))
            return plan
        if op == "record":
            return _record_kwargs(raw.get("options") or {})
        return None


def _record_kwargs(options: dict[str, Any]) -> dict[str, Any]:
    """Only the capture options the console offers, validated here.

    Deliberately narrow. A browser must not be able to choose an arbitrary
    output path, and `run_capture` needs a stopping condition -- a recording
    cannot be cancelled once it starts, so an unbounded one would hold the
    device until the process ended.
    """
    duration_s = options.get("durationS")
    sample_limit = options.get("sampleLimit")
    if duration_s is None and sample_limit is None:
        raise ControlRefused(
            "er_unknown",
            ["BAD_REQUEST", "a recording needs a duration or a sample limit: it cannot be stopped"],
        )
    kwargs: dict[str, Any] = {"overwrite": False}
    if duration_s is not None:
        kwargs["duration_s"] = float(duration_s)
    if sample_limit is not None:
        kwargs["sample_limit"] = int(sample_limit)
    output = options.get("output")
    if output is not None:
        name = str(output)
        if "/" in name or "\\" in name or name.startswith("."):
            raise ControlRefused(
                "er_unknown",
                ["BAD_REQUEST", "a recording is written beside the server, by file name only"],
            )
        kwargs["output"] = name
    tags = options.get("tags")
    if isinstance(tags, dict):
        kwargs["tags"] = {str(k): str(v) for k, v in tags.items()}
    assumed = options.get("assumeVoltageMv")
    if assumed is not None:
        kwargs["assume_voltage_mv"] = int(assumed)
    return kwargs


def create_app(
    supervisor: Supervisor,
    *,
    static_dir: Any,
    allowed_origins: Iterable[str] = (),
    require_token: bool = True,
) -> Starlette:
    console = Console(
        supervisor,
        static_dir=static_dir,
        allowed_origins=allowed_origins,
        require_token=require_token,
    )
    # Kept reachable for tests and for anything embedding the app.
    console.app.state.console = console
    return console.app
