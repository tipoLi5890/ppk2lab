# Web console

A browser console for a PPK2: the live current trace, D0-D7 on the same
timeline, the device's state, and the controls that change it.

> **It cannot show a real device yet.** `0.4.0` ships the built console inside
> the `ppk2lab_web` package and nothing that serves it — no extra to install, no
> command to run, no server. What is in the wheel is the frontend, finished and
> waiting for the supervisor that will own the PPK2 session. `ROADMAP.md` tracks
> what is left.

## What is actually in the release

`pip install ppk2lab` now installs a second top-level package, `ppk2lab_web`.
It contains one accessor and one directory:

```python
from ppk2lab_web import static_dir

static_dir()   # -> Path to index.html, app.js, app.css
```

`static_dir()` raises `FileNotFoundError` rather than returning a directory
that would serve nothing. That is the whole frozen surface; see
[api-baseline.md](api-baseline.md) for what is deliberately not frozen.

## Seeing it today

The frontend runs against a browser-side reproduction of the shipped
`--simulate` profile — the same 100 ms activity cycle, the same 12 mA burst,
the same 9600-baud `TX_DONE` on D0. Simulated numbers are not measurements, and
the console says so on every screen.

Running it needs the frontend sources, which are in the git repository and not
in the distribution:

```bash
git clone https://github.com/tipoLi5890/ppk2lab
cd ppk2lab/webui
npm install
npm run dev
```

The console's `index.html` loads IBM Plex from Google Fonts, so opening it makes
an outbound request. Nothing else about the console talks to the network, and
on an isolated bench the fonts simply fall back to system faces — see
[SECURITY.md](../SECURITY.md).

## What the screen must not soften

These are properties of the instrument, not styling choices. The console
inherits them from the library and has no licence to round any of them up.

- **The PPK2 never measures voltage.** Every voltage shown is a setpoint or an
  explicit assumption, tagged with its `voltage_basis`. In Ampere mode the
  energy figure is `null` — the correct answer, not a failure — until someone
  supplies an assumed voltage. See [energy-analysis.md](energy-analysis.md).
- **Lost samples are drawn, never interpolated over.** A gap is hatched and the
  mean line breaks at it, because joining across one would draw a measurement
  that was never taken. Charge over a window that touched loss is labelled a
  lower bound. See [decimation.md](decimation.md).
- **A decimated bucket carries min, max and mean, never a bare mean.** A mean
  alone hides exactly the current spikes this instrument was bought to see.
- **A quantile served from the 200 nA distribution floor is an upper bound.**
  It is printed with `≤` and marked as bounded. See [faq.md](faq.md).
- **DUT power cannot be read back.** It is reported as requested, never as
  confirmed, and `observed_after` says which.

## Hardware safety in a user interface

The console is a **viewer first**. Opening it changes nothing, and the controls
that can change hardware state are hidden until they are deliberately unlocked.

Beyond that, two rules come from the hardware rather than from taste:

- **A live output comes down before the settings move.** Changing the source
  voltage while VOUT is energised changes what the DUT receives, live. So when
  configuration edits are applied with the output on, the console offers to
  drop the output and apply, or to drop it, apply and bring it back — never to
  change the setting underneath a powered DUT.
- **Arming asks; disarming does not.** Energising VOUT goes through a dry-run
  preview showing the projected before/after and its warning codes, the same
  shape as `configure` without `--apply`. Turning the output off is the
  fail-safe direction and is never behind a dialog.

The console also surfaces what it cannot promise: mode and source-voltage
changes require the device to stop measuring, so it says the stream will break
before it does, and DUT power carries `W_DUT_POWER_TRANSIENT` because VOUT
de-energizes within half a second of the host closing the port.

## How it is built, and why the build is committed

The frontend sources live in `webui/` (React, TypeScript, Vite). `npm run
build` writes into `src/ppk2lab_web/static/`, and **that output is committed**
so that installing from PyPI never requires a Node toolchain.

That decision has a cost: the committed bundle can drift from its sources. CI
rebuilds it on every push and fails if the result differs, and the release
workflow does the same before it builds a distribution — a stale console would
otherwise ship with every other check green.

The bundle contains third-party code (React and its runtime) that no Python
metadata mentions. `THIRD_PARTY_NOTICES.md` covers it, and the build is
configured to keep the upstream licence notices inside the shipped file.

`webui/README.md` documents the architecture for someone changing the frontend:
why sample data does not go through React, how the hierarchical decimator
works, and how the four message catalogues stay in step.

## The planned server

The next piece is the process that owns the one open `ppk2lab.PPK2` session and
feeds the console over a WebSocket. It has to be a single owner because a
serial port is exclusive and because DUT power does not survive the port
closing — a powered measurement has to happen inside one session.

Nothing about its protocol, its dependencies, or how it will be started is
decided yet, so nothing about it is written down here. When it exists it will
be described in this file and frozen in `api-baseline.md`.
