"""`ppk2lab web`: what it refuses before it opens anything, and one real run.

The refusals matter more than they look. Every one of them is a configuration
that would put a control surface somewhere it should not be, and each is caught
before a socket is bound or a device is opened.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import urllib.request

import pytest

from ppk2lab.cli.main import main
from ppk2lab.errors import EXIT_CAPABILITY_MISSING, EXIT_USAGE
from ppk2lab_web.server import READY_PREFIX


def _run(capsys, *argv) -> tuple[int, dict]:
    code = main(["--json", *argv])
    return code, json.loads(capsys.readouterr().out)


def test_control_on_a_non_loopback_address_is_refused(capsys):
    """A viewer may be served anywhere. A control surface may not: there is no
    authentication beyond a per-connection token, and the remediation is the
    one that actually works."""
    code, payload = _run(capsys, "web", "--host", "0.0.0.0", "--allow-control")
    assert code == EXIT_USAGE
    assert payload["ok"] is False
    assert "loopback" in payload["error"]["message"]
    assert "ssh -L" in payload["error"]["remediation"]


def test_control_without_a_token_is_refused(capsys):
    """Loopback is not an authorization boundary: any local process can
    connect, and it arrives with no Origin at all."""
    code, payload = _run(capsys, "web", "--no-token", "--allow-control")
    assert code == EXIT_USAGE
    assert "--no-token" in payload["error"]["message"]


def test_a_viewer_without_a_token_is_allowed(capsys, monkeypatch):
    """The flag exists for a single-user laptop and a bookmarkable URL. It is
    only refused when it would also remove the gate on a control surface."""
    calls: dict = {}

    def fake_serve(**kwargs):
        calls.update(kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr("ppk2lab_web.server.serve", fake_serve)
    # `main` maps KeyboardInterrupt to 130 for every command, so the stand-in
    # raising one is how this test stops the server without running it.
    assert main(["web", "--no-token", "--simulate"]) == 130
    assert calls["require_token"] is False
    assert calls["allow_control"] is False


def test_without_the_extra_the_command_still_exists_and_says_what_is_missing(capsys, monkeypatch):
    """The subcommand has to be in `--help` and in `capabilities` whether or
    not the extra is installed, or the surface a packager reads is a lie. What
    it must not do is fail with an ImportError from three frames down."""
    import importlib.util

    real = importlib.util.find_spec

    def missing(name, *args, **kwargs):
        if name == "websockets":
            return None
        return real(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", missing)
    code, payload = _run(capsys, "web", "--simulate")
    assert code == EXIT_CAPABILITY_MISSING
    assert payload["error"]["code"] == "WEB_EXTRA_MISSING"
    # Named individually: base uvicorn ships no WebSocket implementation, so
    # someone who installed the other two by hand would otherwise get a console
    # that renders and never connects.
    assert "websockets" in payload["error"]["message"]
    assert "pip install 'ppk2lab[web]'" in payload["error"]["remediation"]


def test_the_command_appears_in_the_documented_surface(capsys):
    code, payload = _run(capsys, "capabilities")
    assert code == 0
    web = next(c for c in payload["result"]["commands"] if c["name"] == "web")
    assert web["state_changing"] is True
    assert "--allow-control" in web["description"]


@pytest.mark.slow
def test_a_real_server_serves_the_console_and_shuts_down_cleanly(tmp_path):
    """One end-to-end run over a real socket.

    Everything else here is in-process. This is the one that proves the pieces
    fit: the port is bound before the device is opened, the ready line carries
    the chosen port (which is what makes `--http-port 0` usable), the console
    is served, and a signal ends it with the device closed.
    """
    flags = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {}
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "ppk2lab.cli.main",
            "--json",
            "--simulate",
            "web",
            "--http-port",
            "0",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=tmp_path,
        **flags,
    )
    try:
        ready = None
        assert proc.stderr is not None
        for line in proc.stderr:
            if line.startswith(READY_PREFIX):
                ready = json.loads(line[len(READY_PREFIX) :])
                break
        assert ready is not None, "the server never announced a bound port"
        assert ready["port"] != 0
        assert ready["simulated"] is True
        assert ready["control_enabled"] is False

        with urllib.request.urlopen(ready["url"], timeout=10) as response:
            body = response.read()
        assert response.status == 200
        assert b"app.js" in body

        sig = signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT
        proc.send_signal(sig)
        assert proc.wait(timeout=30) == 0, "a clean shutdown did not exit 0"
        assert proc.stdout is not None
        envelope = json.loads(proc.stdout.read())
        assert envelope["ok"] is True
        assert envelope["command"] == "web"
        assert envelope["result"]["control_enabled"] is False
        assert envelope["result"]["simulated"] is True
        # A simulated session says so in the envelope, not only on screen.
        assert any(w["code"] == "W_GENERIC" for w in envelope["warnings"])
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


@pytest.mark.skipif(
    not hasattr(signal, "SIGUSR1"), reason="needs a spare catchable signal to stand in"
)
def test_ctrl_break_is_made_to_raise_the_way_ctrl_c_does(monkeypatch):
    """The mechanism behind a clean Windows shutdown, exercised anywhere.

    uvicorn restores the handler that was installed before it and then
    re-raises the signal, so whether `serve` gets its `KeyboardInterrupt`
    depends entirely on what that restored handler does. On SIGINT it is
    Python's `default_int_handler` and it raises. `SIGBREAK` has no such
    handler -- Python leaves it at the C runtime's default, which terminates
    the process with exit code 3 -- so a console stopped with Ctrl-Break died
    before it could print the session's audit record.

    `SIGBREAK` only exists on Windows, so the substitution is what makes this
    checkable on the platforms most of the work happens on: the helper reads
    the name off the `signal` module, and any catchable signal proves the same
    sequence. The real thing is covered on Windows by the end-to-end test
    above, which is where the bug was found.
    """
    from ppk2lab_web import server

    monkeypatch.setattr(signal, "SIGBREAK", signal.SIGUSR1, raising=False)
    original = signal.getsignal(signal.SIGUSR1)
    try:
        with server._ctrl_break_raises_keyboard_interrupt():
            assert signal.getsignal(signal.SIGUSR1) is signal.default_int_handler
            # Not just the assignment: the sequence uvicorn actually performs.
            with pytest.raises(KeyboardInterrupt):
                signal.raise_signal(signal.SIGUSR1)
        # And it puts back what it found, so nothing outside this server's run
        # inherits an interrupt handler it never asked for.
        assert signal.getsignal(signal.SIGUSR1) is original
    finally:
        signal.signal(signal.SIGUSR1, original)


def test_without_the_signal_the_helper_does_nothing(monkeypatch):
    """On a platform with no `SIGBREAK` -- every one but Windows -- it is a
    no-op rather than an error, and it never touches `SIGINT`."""
    from ppk2lab_web import server

    monkeypatch.delattr(signal, "SIGBREAK", raising=False)
    before = signal.getsignal(signal.SIGINT)
    with server._ctrl_break_raises_keyboard_interrupt():
        assert signal.getsignal(signal.SIGINT) is before
    assert signal.getsignal(signal.SIGINT) is before
