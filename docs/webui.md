# Web console

A browser console for a PPK2: the live current trace, D0-D7 on the same
timeline, the device's state, and the controls that change it.

```bash
pip install 'ppk2lab[web]'
ppk2lab web                    # http://127.0.0.1:8765/, viewer only
ppk2lab web --allow-control    # …and it can change device state
ppk2lab --simulate web         # no hardware; everything on screen is synthetic
```

## What runs, and what owns the device

`ppk2lab web` opens **one** `PPK2` session and holds it for the life of the
process. It has to: a serial port is exclusive, and DUT power does not survive
the port closing, so a powered measurement can only happen inside one session.

Two consequences follow, and both matter more than they look:

- While the server runs, any other `ppk2lab` command against the same unit gets
  `PORT_BUSY` (exit 4). So does the Nordic Power Profiler application.
- **Closing the browser tab does not de-energise VOUT.** Only stopping the
  server does — and stopping it restores the power state the session started
  with, fail-safe OFF when that was unknown.

Everything the device does is driven from one thread. `PPK2` is not safe to use
from two, and the lock it does have exists to *refuse* a second reader rather
than to allow one: the 4-byte sample words carry no sync word, so two readers
taking turns on one port would split words between them and neither would
notice.

## Read-only by default

Without `--allow-control` the operations that would reach the device are not
registered at all. There is no code path to refuse, which is a stronger
statement than a refusal and one a test can check by handing the server a
device that fails if it is ever asked to change state.

A dry run is still available on a locked server. It writes nothing, and
refusing it would only stop an operator seeing what a change *would* do before
deciding whether to restart with control on.

With `--allow-control`, the console can set mode, source voltage and DUT power
— the same three things `configure` can, through the same methods, with the
same validation and the same `--max-voltage-mv` ceiling. It can **never**:

- reset the device, or set a user gain (which would silently rescale every
  later reading);
- choose which device to open — `--device` and `--port` are process arguments,
  and the difference between the operator choosing the instrument and the page
  choosing it is the whole point;
- read or write a file, beyond a recording whose name it supplies and whose
  directory it does not;
- raise the session's voltage ceiling. It may lower it for itself; the server
  re-validates every request regardless.

## What the screen must not soften

These are properties of the instrument, not styling choices. The console
inherits them from the library and has no licence to round any of them up.

- **The PPK2 never measures voltage.** Every voltage shown is a setpoint or an
  explicit assumption, tagged with its `voltage_basis`. In Ampere mode the
  energy figure is `null` — the correct answer, not a failure — until someone
  supplies an assumed voltage. See [energy-analysis.md](energy-analysis.md).
- **Lost samples are drawn, never interpolated over.** A gap is hatched and the
  mean line breaks at it, because joining across one would draw a measurement
  that was never taken. See [decimation.md](decimation.md).
- **A decimated bucket carries min, max and mean, never a bare mean.** A mean
  alone hides exactly the current spikes this instrument was bought to see.
- **A quantile served from the 200 nA distribution floor is an upper bound.**
  It is printed with `≤` and marked as bounded. See [faq.md](faq.md).
- **DUT power cannot be read back.** It is reported as requested, never as
  confirmed, and `observed_after` says which.
- **An unknown mode is shown as unknown.** The mode decides whether energy is
  computable at all, so guessing it would put a number behind an assumption
  nothing supports.

Two more come from the console being a *remote* view rather than the
instrument itself:

- **A dropped connection is not a stopped measurement.** The badge says the
  link is down, the trace freezes where it froze, and the device is very likely
  still running. `measuring: true` beside a reconnecting badge is the normal
  reading of "it is still going and this console cannot see it".
- **What this browser failed to receive is counted apart from what the
  instrument failed to deliver.** A slow link produces a hatched span like a
  sample gap does, but it is tallied separately, because letting one look like
  the other would let a busy laptop look like a lossy instrument.
- **A slow link costs samples, never the distribution.** Only sample frames are
  discarded when a console falls behind. The distribution grid is absolute
  state, so the newest one survives the discard and the panel keeps updating
  — a frozen one, with the rest of the screen moving, would read as a settled
  measurement rather than as a frame that never arrived.

## Safety in a user interface

The console is a **viewer first**. Opening it changes nothing, and the controls
that can change hardware state are hidden until they are deliberately unlocked.

Beyond that, three rules come from the hardware rather than from taste:

- **A live output comes down before the settings move.** Changing the source
  voltage while VOUT is energised changes what the DUT receives, live. So when
  configuration edits are applied with the output on, the console offers to
  drop the output and apply, or to drop it, apply and bring it back — never to
  change the setting underneath a powered DUT.
- **Arming asks; disarming does not.** Energising VOUT goes through a dry-run
  preview showing the projected before/after and its warning codes, the same
  shape as `configure` without `--apply`. Turning the output off is the
  fail-safe direction, is never behind a dialog, and never queues behind
  anything else on the server either.
- **A preview is a real dry run.** The plan is sent to the device with
  `dry_run=True` first, so the before/after and the warnings in the dialog are
  what the device said rather than what the page guessed. The reply carries a
  sequence number that is echoed back on apply: a console that previewed, lost
  its connection and came back cannot apply a plan against a device in a
  different state than the one it showed.

The console also surfaces what it cannot promise: mode and source-voltage
changes require the device to stop measuring, so it says the stream will break
before it does, and DUT power carries `W_DUT_POWER_TRANSIENT` because VOUT
de-energizes within half a second of the host closing the port.

## Recording

The recording panel writes a real `.ppk2a` through `run_capture`, so the
artifact is the library's — stats, manifest, timeline cross-check, checksum —
rather than a second implementation of any of it. The live trace keeps drawing
while it runs, fed from the capture's own blocks, so what is on screen and what
is on disk are the same samples.

A recording **cannot be stopped once it starts**: `run_capture` has no
cooperative stop. It is bounded by the duration you set, the console says so
before the button, and the server refuses an unbounded one outright.

The file name is a name, not a path. A browser does not choose where the server
writes.

## The wire

One WebSocket at `/ws` carries both planes. Text frames are JSON — state, the
session log, commands and their replies. Binary frames are samples, and they
are binary for three reasons in order of weight:

1. `Calibration.convert_block` produces `NaN` for a range with no constants and
   for an unknown source voltage, and JSON has no NaN. Every workaround is an
   encoding invented to paper over a format that cannot say what happened.
2. Tier 0 is a thousand buckets a second: 24 bytes each packed, against roughly
   130 and an object allocation each as JSON.
3. The console stores buckets in parallel typed arrays, so a struct-of-arrays
   frame decodes into views over the received buffer rather than through a
   thousand JavaScript numbers per second.

The protocol carries an integer version. A console that does not speak it stops
at the handshake, before a single binary frame arrives, and a frame that fails
to decode stops it too — the bundle ships inside the wheel and nothing rebuilds
it at install time, so an old console meeting a new server is ordinary, and a
changed layout read as numbers would be a fabricated current trace.

**All control rides that socket, and all HTTP is GET.** That is a security
decision rather than a stylistic one: a WebSocket is not subject to the
same-origin policy, so the handshake has to check the origin and a
per-connection token regardless — and once that gate exists, putting the
commands behind it leaves no state-changing HTTP endpoint for a cross-origin
page to target. See [../SECURITY.md](../SECURITY.md).

### What a late-joining console sees

Every viewer sees the same live stream; only one connection at a time may
command the device, and the lease is released when that console goes away.

A console that attaches after the session started gets the current state, the
session log so far, the distribution grid, and whatever buckets have not been
sent yet — **not** the whole history the first console has drawn. The retained
history lives in the browser's own rings, and this is a real limitation rather
than an oversight: shipping four tiers of backlog on connect is additive and
has not been done.

### Two grids, deliberately different

The console's distribution grid is 24 bins per decade over seven decades; the
one behind `ppk2lab measure` is 128 per decade over the instrument's whole
span. Same 200 nA floor, same underflow bin, same "a quantile from bin 0 is an
upper bound" meaning — different resolution. So a p50 on this screen is coarser
than a p50 from the CLI on the same data. That is defensible for a live display
and is written down here so the first person to compare the two numbers does
not file a bug against the wrong component.

## Working on the frontend

The frontend sources live in `webui/` (React, TypeScript, Vite). `npm run
build` writes into `src/ppk2lab_web/static/`, and **that output is committed**
so that installing from PyPI never requires a Node toolchain.

That decision has a cost: the committed bundle can drift from its sources. CI
rebuilds it on every push and fails if the result differs, and the release
workflow does the same before it builds a distribution — a stale console would
otherwise ship with every other check green.

```bash
cd webui
npm install
npm run dev              # the simulated console, no server needed
```

`npm run dev` alone runs against a browser-side reproduction of the shipped
`--simulate` profile. To drive a live server instead, start it with
`ppk2lab web --allow-origin http://localhost:5273` and open
`http://localhost:5273/?source=ws` — Vite proxies `/ws` to `127.0.0.1:8765`, so
the URL rule is identical in development and in production. The extra origin
is needed because the handshake accepts only the server's own by default. A
server on another machine is `?ws=wss://bench.local:8765/ws`, which is a
runtime question rather than a build-time one; the shipped bundle contains no
host and no port at all.

The console's `index.html` loads IBM Plex from Google Fonts, so opening it makes
an outbound request. The *server* talks to a serial port and its own listener
and to nothing else; the *browser*, on a page the server handed it, fetches the
fonts. On an isolated bench they fail and system faces are used.

`webui/README.md` documents the architecture for someone changing the frontend.

## What has not been proven

Nothing here has been run against a physical PPK2. Every test above uses the
simulator, which exercises the protocol and the state machine but not the
instrument. `ROADMAP.md` records what closing that takes.
