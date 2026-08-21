"""Starting the console, and stopping it without leaving a DUT energised.

The listening socket is bound here rather than by uvicorn, for two reasons that
both matter. It makes ``--http-port 0`` usable -- the real port is known before
anything starts, so it can be printed and a supervising process can read it --
and it puts the ``OSError`` in reach, so "that address is taken" becomes an
error with a remediation instead of a traceback from inside a web server.
"""

from __future__ import annotations

import atexit
import contextlib
import json
import signal
import socket
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ppk2lab.errors import ListenAddressInUseError, PermissionDeniedError

from . import static_dir
from .supervisor import Supervisor

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable, Iterator

    from ppk2lab.device import PPK2

#: Printed on stderr once the socket is bound, always. `--json` sends the
#: envelope to stdout when the command *ends*, which is no use to something
#: that needs the URL while it runs.
READY_PREFIX = "ppk2lab-web-ready "


@dataclass
class ServeResult:
    """What the session did, for the command's JSON envelope."""

    url: str
    host: str
    port: int
    control_enabled: bool
    token_required: bool
    simulated: bool
    device: dict[str, Any] = field(default_factory=dict)
    listened_s: float = 0.0
    shutdown_reason: str = "signal"
    state_changes: list[dict[str, Any]] = field(default_factory=list)
    counters: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "host": self.host,
            "port": self.port,
            "control_enabled": self.control_enabled,
            "token_required": self.token_required,
            "simulated": self.simulated,
            "device": self.device,
            "listened_s": round(self.listened_s, 3),
            "shutdown_reason": self.shutdown_reason,
            "state_changes": self.state_changes,
            "counters": self.counters,
        }


def bind(host: str, port: int) -> socket.socket:
    """Bind and listen, translating the two failures a person can act on."""
    sock = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
        sock.listen(64)
    except PermissionError as exc:
        sock.close()
        raise PermissionDeniedError(
            f"not allowed to listen on {host}:{port}",
            remediation="Ports below 1024 need elevated privileges on most systems. "
            "Choose a port above 1024 with --http-port.",
        ) from exc
    except OSError as exc:
        sock.close()
        raise ListenAddressInUseError(f"cannot listen on {host}:{port}: {exc}") from exc
    return sock


def serve(
    *,
    open_device: Callable[[], PPK2],
    host: str = "127.0.0.1",
    port: int = 8765,
    allow_control: bool = False,
    require_token: bool = True,
    allow_origin: tuple[str, ...] = (),
    open_browser: bool = False,
    autostart: bool = True,
    history_seconds: float = 10.0,
    announce: bool = True,
) -> ServeResult:
    """Run the console until the process is asked to stop.

    Blocks. Returns what the session did, so ``ppk2lab web`` can render an
    ordinary envelope like every other command rather than inventing a
    different contract for the one command that happens to be long-lived.
    """
    import uvicorn

    from .app import create_app

    assets = static_dir()  # before the socket: a console that 404s its own
    # JavaScript is harder to diagnose than one that refuses to start.
    sock = bind(host, port)
    bound_host, bound_port = sock.getsockname()[:2]
    url = f"http://{_display_host(bound_host)}:{bound_port}/"

    origins = {url.rstrip("/"), f"http://{bound_host}:{bound_port}"}
    origins.update(o.rstrip("/") for o in allow_origin)

    supervisor = Supervisor(
        open_device=open_device,
        publish=lambda message, target: None,  # replaced below, once the hub exists
        allow_control=allow_control,
        autostart=autostart,
        history_seconds=history_seconds,
    )
    app = create_app(
        supervisor,
        static_dir=assets,
        allowed_origins=origins,
        require_token=require_token,
    )
    console = app.state.console
    supervisor.set_publisher(console.hub.publish)

    # Belt, braces, and a second pair of braces. The cost of missing this is a
    # DUT left energised, so it is registered before anything can fail.
    atexit.register(supervisor.shutdown)

    started = time.monotonic()
    result = ServeResult(
        url=url,
        host=str(bound_host),
        port=int(bound_port),
        control_enabled=allow_control,
        token_required=require_token,
        simulated=False,
    )
    try:
        supervisor.start()
    except BaseException:
        sock.close()
        supervisor.shutdown()
        raise

    snapshot = supervisor.snapshot
    result.device = snapshot.get("device", {})
    result.simulated = bool(result.device.get("simulated"))

    if announce:
        _announce(result)
    if open_browser:
        with contextlib.suppress(Exception):
            webbrowser.open(url)

    config = uvicorn.Config(
        app,
        # Explicit rather than "auto": a missing websockets package would
        # otherwise become a 404 on /ws with only a log line to say why, and a
        # console that renders and never connects is the worst way to find out.
        ws="websockets-sansio",
        log_level="warning",
        access_log=False,
        # A browser only ever sends small JSON here; the sample plane is
        # one-way. Nothing legitimate is near this.
        ws_max_size=64 * 1024,
    )
    server = uvicorn.Server(config)
    try:
        with _ctrl_break_raises_keyboard_interrupt():
            server.run(sockets=[sock])
    except KeyboardInterrupt:
        # Expected, and not a failure. uvicorn catches SIGINT, shuts down
        # gracefully, and then re-raises the signal so that a caller sees the
        # conventional interrupted behaviour -- which for most programs is
        # right. For this one it is not: a command whose whole job is to run
        # until it is stopped has *finished* when it is stopped, and the
        # session's audit record is the deliverable. Exiting 130 here would
        # throw it away at exactly the moment it exists.
        result.shutdown_reason = "signal"
    finally:
        supervisor.shutdown()
        with contextlib.suppress(Exception):
            atexit.unregister(supervisor.shutdown)
        with contextlib.suppress(Exception):
            sock.close()

    result.listened_s = time.monotonic() - started
    result.state_changes = supervisor.close_changes
    result.counters = supervisor.snapshot.get("counters", {})
    if not supervisor.device_present:
        result.shutdown_reason = "device_lost"
    return result


def _display_host(host: str) -> str:
    return f"[{host}]" if ":" in host else host


@contextlib.contextmanager
def _ctrl_break_raises_keyboard_interrupt() -> Iterator[None]:
    """Make Ctrl-Break end this server the way Ctrl-C does. Windows only.

    uvicorn handles every signal in its ``HANDLED_SIGNALS`` -- on Windows that
    includes ``SIGBREAK`` -- shuts down gracefully, restores the handler that
    was installed before it, and then re-raises the signal so a caller sees the
    conventional interrupted behaviour. That last step is why ``serve`` catches
    ``KeyboardInterrupt``: on SIGINT the restored handler is Python's
    ``default_int_handler``, which raises one.

    ``SIGBREAK`` has no such handler. Python installs ``default_int_handler``
    for ``SIGINT`` and leaves ``SIGBREAK`` at the C runtime's default, which
    terminates the process rather than raising anything. Observed: exit 3, on
    all four Windows jobs. So a console stopped with Ctrl-Break died at
    ``raise_signal``, and the session's audit record -- the deliverable of a
    command whose job is to run until it is stopped -- was thrown away.

    Installing ``default_int_handler`` here means the handler uvicorn restores
    is one that raises, so both keys reach the same ``except`` clause. It
    changes nothing on any other platform, and nothing about the shutdown
    itself: the device is closed by the lifespan hook well before this, which
    is why the bug cost an exit code and a record rather than a de-energised
    DUT.
    """
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is None or threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.signal(sigbreak, signal.default_int_handler)
    try:
        yield
    finally:
        with contextlib.suppress(ValueError, OSError):
            signal.signal(sigbreak, previous)


def _announce(result: ServeResult) -> None:
    """Say where it is, on stderr, whatever the output format.

    stderr because stdout belongs to the envelope, and the envelope is printed
    when the command ends -- which is exactly when the URL stops being useful.
    """
    print(f"ppk2lab web: {result.url}", file=sys.stderr)
    if result.simulated:
        print(
            "  simulated device: everything on screen is synthetic, not measured",
            file=sys.stderr,
        )
    if result.control_enabled:
        print(
            "  control is ENABLED: this page can change mode, voltage and DUT power",
            file=sys.stderr,
        )
    else:
        print("  viewer only: pass --allow-control to change device state", file=sys.stderr)
    print("  press Ctrl-C to stop; the device is closed and its power restored", file=sys.stderr)
    print(
        READY_PREFIX
        + json.dumps(
            {
                "host": result.host,
                "port": result.port,
                "url": result.url,
                "control_enabled": result.control_enabled,
                "simulated": result.simulated,
            },
            sort_keys=True,
        ),
        file=sys.stderr,
        flush=True,
    )
