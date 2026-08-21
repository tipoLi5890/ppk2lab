# Security model

This document describes the threat model and hardening rules for ppk2lab
across five surfaces: hardware control, USB/serial input, capture files,
untrusted analysis input, and — new in `0.5.0` — the local listener
`ppk2lab web` opens, which is recorded at the end.

## Hardware safety (protecting the DUT and operator)

The most damaging failure this tool can cause is powering or over-volting a
device under test. Hard rules, enforced in code and tests:

- No code path enables DUT power, changes source voltage, changes mode, or
  resets the device implicitly. Installation, discovery, `doctor`, and
  `capture` never do; `configure` is a dry run without `--apply`.
- Voltage is always explicit millivolts (`voltage_mv`); bare or unit-less
  values are rejected, and the range (800-5000 mV) is validated before any
  byte reaches the wire.
- Every state change records requested/before/after state and whether the
  after state was read back from the device.
- Sessions restore the starting DUT power state on close. When the starting
  state was unknown, power is set OFF as a fail-safe and the decision is
  recorded, never silent.
- There is no `--yes-to-everything` option and no public API to bypass
  voltage limits.

## USB / serial input (untrusted device data)

Data arriving from the serial port is treated as untrusted:

- The sample parser is bounded-memory, tolerates arbitrary chunk sizes,
  and treats malformed values (invalid range fields) as flagged data, never
  as crashes.
- Metadata parsing never raises on malformed content; unknown keys, NaN, and
  truncation are preserved and reported as warnings.
- The simulator (used with `--simulate`) refuses unknown opcodes loudly so
  protocol drift is caught in tests.

## Capture files (untrusted file input)

`.ppk2a` files may come from other machines or users:

- Readers validate the container, format version, sample encoding, chunk
  sizes, CRC32 per chunk, and the SHA-256 of the sample data before use.
- A file with a newer `format_version` is refused explicitly rather than
  misread.
- Malformed annotation JSONL is rejected with the line number; entries are
  never partially applied.
- Writers are atomic (temp file + rename) and never overwrite an existing
  file without `--overwrite`.

## Analysis input (rules, specs, CLI arguments)

- Assertion rules and trigger specs are parsed with strict grammars; parse
  failures return `INVALID_ARGUMENT` with remediation, never arbitrary
  evaluation. There is no `eval` of user input anywhere.
- Data loss can never produce a false success: assertion windows that
  overlap gaps return status `incomplete` (exit 6), and decoders emit
  zero-confidence error annotations rather than fabricated data.

## The web console's listener

`ppk2lab web` is the project's first listening socket. Everything else in the
project talks to a USB serial port and to nothing else.

**It binds `127.0.0.1` by default**, and two combinations are refused rather
than warned about:

- `--allow-control` on a non-loopback `--host`. There is no authentication
  beyond a per-connection token, and the answer that actually works is an
  authenticated tunnel: `ssh -L 8765:127.0.0.1:8765 <bench-host>`.
- `--allow-control` together with `--no-token`. Loopback is not an
  authorization boundary: any local process can connect, and it arrives with no
  `Origin` header at all.

**The origin rule is split by what the endpoint hands out.** A *foreign*
origin is refused everywhere: the threat is a page on another site opening a
socket to this one, and a page always sends one. An *absent* origin is a
different caller — a local script, no browser involved. The WebSocket refuses
it, because that is where the per-connection token is issued and where every
state change goes. The read-only snapshot at `GET /api/session` admits it,
because a script gains nothing there it could not get from `ppk2lab info` when
the server is not holding the port, and refusing it would leave no way at all
to read device state while it is.

**Every state change rides the WebSocket, and all HTTP is GET.** A WebSocket is
not subject to the same-origin policy — any page can open one and the browser
sends `Origin` without enforcing it — so the handshake validates the origin and
a per-connection token regardless. Once that gate exists, putting the commands
behind it leaves no state-changing HTTP endpoint for a cross-origin page to
target, and a page on `evil.example` that re-resolves to 127.0.0.1 arrives with
its own origin and is refused.

**What `--allow-control` permits** is exactly what `configure` can do: set
mode, set source voltage, set DUT power. Through the same methods, with the
same validation, under the same `--max-voltage-mv` ceiling, each recorded as a
`StateChange` broadcast to every attached console. What it never permits, in
any configuration:

- `reset()` and `set_user_gain()` — no route to them exists.
- Choosing the device. `--device` and `--port` are process arguments; a page
  must not be able to say which instrument to open.
- Filesystem access. A recording supplies a file name, never a path, and the
  static handler serves the built console directory and nothing else.
- Raising the session's voltage ceiling. A console may lower it for itself.
- Stopping the server, or running any other command.

**Back-pressure never buffers without limit.** A console that stops reading has
its queued sample frames discarded rather than accumulated, and is told exactly
which stretch of the timeline it will not receive — which it then draws as a
gap and counts separately from instrument loss. A 400 kB/s producer against a
slow consumer would otherwise be an OOM, and silently thinning a trace is the
failure this project exists to prevent.

Sample frames are the only thing discarded. The distribution grid is absolute
state rather than a stretch of timeline, so there is nothing to report about
dropping it and nothing saved by doing so; the newest one survives a discard.
Control messages are never dropped at all — a console that silently missed a
state change would show hardware state that had stopped being true, so past
capacity the connection is closed instead.

**The exclusive session has a safety asymmetry worth stating twice.** The
server holds the port for the life of the process, which is what makes a
powered measurement possible from a UI at all — and it means **closing the
browser tab does not de-energise VOUT. Only stopping the server does.** Session
close then restores the starting power state, fail-safe OFF when it was
unknown.

**Google Fonts.** The console's `index.html` loads IBM Plex from
`fonts.googleapis.com` / `fonts.gstatic.com`. The *server* makes no outbound
request; the *browser*, on a page the server handed it, fetches the fonts. On
an isolated bench they fail and the console falls back to system faces.

## Reporting a vulnerability

Report security issues through
[GitHub Issues](https://github.com/tipoLi5890/ppk2lab/issues) (or a private
security advisory on the repository once published). Include the ppk2lab
version, OS, and reproduction steps. Hardware-safety regressions (anything
that could power a DUT unexpectedly) are treated at the highest severity.
