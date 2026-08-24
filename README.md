# ppk2lab

**English** · [繁體中文](https://github.com/tipoLi5890/ppk2lab/blob/main/.github/README.zh-Hant.md) · [简体中文](https://github.com/tipoLi5890/ppk2lab/blob/main/.github/README.zh-Hans.md) · [日本語](https://github.com/tipoLi5890/ppk2lab/blob/main/.github/README.ja.md)

**See exactly where your device's power goes.** `ppk2lab` turns the Nordic Power Profiler Kit II into a scriptable measurement lab: record current and eight digital signals on one timeline, decode low-speed UART and SPI, and attribute energy to individual protocol events. Everything is reachable from Python, the command line, or an AI agent, so a power regression can fail a CI build the same way a failing test does.

[![CI](https://github.com/tipoLi5890/ppk2lab/actions/workflows/ci.yml/badge.svg)](https://github.com/tipoLi5890/ppk2lab/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/ppk2lab)](https://pypi.org/project/ppk2lab/)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Status](https://img.shields.io/badge/status-experimental-orange)

> [!IMPORTANT]
> This project is experimental and is not affiliated with or endorsed by Nordic Semiconductor ASA.

## Highlights

`ppk2lab` lets humans and agents:

- discover and configure one or more PPK2 devices;
- capture calibrated current and all D0-D7 digital states on one synchronized timeline;
- decode low-speed UART and SPI traffic (9,600 baud and 10 kHz SCLK are the top rate tier);
- measure charge, energy, peak current, latency, and the distribution (p50/p90/p99) of a window or a decoded event, with a typical per-range error bar;
- work with hour-scale captures: read a manifest without its samples, read one window, or export a decimated summary;
- trigger captures from current, digital state, UART content, or SPI transactions;
- run reproducible power assertions in local automation and CI;
- watch a live device in a browser: `ppk2lab web` serves a console with the current trace, D0-D7 on the same timeline, and — only with `--allow-control` — the controls that change device state;
- say when the samples cannot support an answer — dropped samples, ADC saturation, a window with no data in it — instead of returning a confident number;
- try everything without hardware via `--simulate`.

## Install

The core package requires Python 3.11 or newer. `pyserial` is the only runtime dependency. The browser console needs a server, and that server is opt-in: `pip install 'ppk2lab[web]'`.

```bash
pipx install ppk2lab        # recommended for the CLI
# or inside a virtual environment:
pip install ppk2lab

ppk2lab --version
ppk2lab doctor --json

# optional: the browser console
pip install 'ppk2lab[web]'
ppk2lab --simulate web      # http://127.0.0.1:8765/, no hardware needed
```

`0.5.1` is the current release and `0.2.0` was the first stable one, so a plain `pip install ppk2lab` resolves it. It is labelled experimental on purpose: the toolchain is heavily tested without hardware, and the properties that keep a measurement honest are enforced rather than assumed — but the hardware validation behind it is partial. One unit, macOS only, and no decoder has yet read a real signal. [Roadmap](#roadmap) says exactly what that covers and what it does not; read it before you trust a number.

Run from a development checkout:

```bash
git clone https://github.com/tipoLi5890/ppk2lab
cd ppk2lab
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
ppk2lab doctor --json
```

### Claude Code plugin

After installing the `ppk2lab` CLI, add this repository as a Claude Code marketplace and install its skills:

```text
/plugin marketplace add tipoLi5890/ppk2lab
/plugin install ppk2lab@ppk2lab
```

During plugin development, load the checkout directly with `claude --plugin-dir ./`.

### Codex plugin and loose skills

The skills in `skills/` are plain `SKILL.md` files that Codex can load directly; a Codex plugin manifest is not published yet, so use the loose-skill path below.

For repository-scoped loose skills, place them in `.agents/skills/`. For a user-wide installation:

```bash
mkdir -p ~/.agents/skills
cp -R skills/* ~/.agents/skills/
```

Claude Code can use the same skill sources from `.claude/skills/` or through the bundled plugin. Full setup and troubleshooting lives in [INSTALL.md](https://github.com/tipoLi5890/ppk2lab/blob/main/INSTALL.md).

## Quickstart

Every command also runs without hardware by adding `--simulate`, which uses a built-in simulated PPK2 to exercise the toolchain. A simulated run is not a measurement.

Inspect the environment and connected devices without changing hardware state:

```bash
ppk2lab doctor --json
ppk2lab discover --json
ppk2lab info --device <serial> --json
```

Capture current and D0-D7. `capture` never enables DUT power:

```bash
ppk2lab capture --device <serial> --duration 5s --digital D0-D7 --output capture.ppk2a
```

See what the artifact actually holds — timeline, gaps, warnings, calibration — without loading a sample:

```bash
ppk2lab inspect capture.ppk2a --json
```

Decode a supported low-speed UART signal and measure energy by frame:

```bash
ppk2lab decode capture.ppk2a --uart D0 --baud 9600 --output uart.jsonl
ppk2lab measure capture.ppk2a --annotations uart.jsonl --group-by frame --json
```

Run a reproducible power assertion:

```bash
ppk2lab assert capture.ppk2a \
  --rule 'p99_current < 15mA' \
  --rule 'after uart("TX_DONE"), within 20ms, avg_current < 10uA' \
  --format json
```

For a CI threshold, prefer a percentile to `max_current`: range switches accumulate as a capture runs longer, so a maximum drifts upward with capture length while `p99_current` does not.

These commands are implemented today. Every JSON contract carries `schema_version` 1 and has followed the stability policy in [docs/SPEC.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/SPEC.md) since `0.2.0`: additions are backward compatible, and a removal or a change of meaning needs a `schema_version` bump and a migration note.

## What it does

| Command | Purpose | Hardware state |
|---|---|---|
| `discover`, `info` | Find devices and inspect firmware, metadata, calibration, and current state | Read-only |
| `capabilities`, `schema` | Return machine-readable commands, limits, decoders, coded warning/gap/interruption catalogs, and JSON schemas | Read-only |
| `doctor` | Diagnose USB permissions, busy ports, metadata, calibration, and stream rate; exits nonzero on a failing check | Read-only by default |
| `configure` | Select mode, source voltage, and DUT power with before/after state reporting | State-changing; a dry run without `--apply` |
| `capture` | Record raw current, range, sequence counter, and D0-D7 with triggers | Measurement; never enables DUT power |
| `inspect` | Read a capture's manifest — timeline, gaps, warnings, calibration — without loading its samples | Offline |
| `decode` | Produce UART, SPI, edge, pulse, and transaction annotations | Offline |
| `measure` | Current, charge, energy, peaks, percentiles, and an optional duty-cycle split for a time range or annotation | Offline |
| `assert` | Apply reproducible power and protocol regression rules | Offline |
| `compare` | Difference two captures on one metric, with an error bar that says whether the instrument's gain error cancelled | Offline |
| `export` | Create CSV, VCD, JSONL, windowed, and decimated views without replacing raw evidence | Offline |
| `web` | Serve the browser console on a local HTTP + WebSocket listener | Viewer; state-changing only with `--allow-control` |

## Scope and physical limits

PPK2 samples its digital inputs at 100 kS/s. Protocol decoding is therefore intended for low-speed signals. Tier boundaries are samples per bit at that fixed rate — 10, 5, and 2.5 — and nothing else. The top tier covers:

- UART up to 9,600 baud;
- SPI clock up to 10 kHz;
- all eight digital inputs D0-D7;
- higher rates only when explicitly marked conditional or experimental.

This project is not intended to replace a MHz-class logic analyzer. A frame that crosses a missing-sample interval is reported as incomplete or invalid, never as a confident decode.

Every capture reports the samples it lost. Most of the loss `0.2.0` measured turned out to be self-inflicted: the artifact writer compressed each 4 MB chunk on the thread feeding samples in, and the ~290 ms pause overflowed the reader's queue. That is why those 60-second captures each lost exactly five whole chunks — one per chunk boundary crossed. Compression now runs on its own thread and the reader's queue is bounded in bytes rather than in reads, and on the same host six consecutive captures (5 × 30 s and 1 × 60 s) recorded every sample: no gaps, `complete: true`. What the host itself costs under load is now unquantified — it was never separated from the writer's own stalls — so treat loss as possible, reported, and no longer expected.

> [!IMPORTANT]
> **One known load has now been measured; a calibrated reference still has not.** A 680 kΩ ±5% resistor between VOUT and GND, at one unit's existing 3700 mV setpoint, should draw 5.4332 µA through the meter's own 1000.625 Ω shunt; eleven captures measured 5.55 µA (5.5280–5.5614 µA), +2.1% from nominal. The resistor's own tolerance puts the true current anywhere in 5.175–5.719 µA, so that check rules out a gross error and cannot resolve the instrument's own gain error — resolving it needs a resistor an order of magnitude tighter, or a calibrated reference. Every accuracy figure here still restates Nordic's *typical* per-range specification, and uncertainty is reported as `guaranteed: false` for exactly that reason. That session covered one unit, one firmware fingerprint, macOS only, and no MCU fixture, so the UART and SPI tiers below remain samples-per-bit budgets rather than measured error rates. [ROADMAP.md](https://github.com/tipoLi5890/ppk2lab/blob/main/ROADMAP.md) lists what is still unmeasured.

| Protocol | Rate tier |
|---|---|
| UART at 1,200-9,600 baud | validated |
| UART at 19,200 baud | conditional |
| UART at 38,400 baud | experimental (needs `--allow-experimental`) |
| UART at 57,600 baud and above | unsupported — refused |
| SPI clock up to 10 kHz | validated |
| SPI at 10-20 kHz | conditional |
| SPI at 20-40 kHz | experimental; above 40 kHz refused |

## Hardware safety

- `configure` is a dry run unless `--apply` is given.
- `capture` never enables DUT power and has no option that would — and neither does an earlier `configure --dut-power on --apply`, because the PPK2 de-energizes VOUT once the host closes the serial port. Measured on one unit: still live at 0 ms of closure, a coin flip at 100-250 ms, off every time from 500 ms on. Powering a DUT through the meter therefore has to happen inside the same open session that captures (`ppk2lab.PPK2` in Python); `configure --dut-power on --apply` warns `W_DUT_POWER_TRANSIENT` rather than implying otherwise.
- Source voltage is expressed as `voltage_mv`, checked against device capabilities, and never inferred from a DUT name.
- Mode changes, DUT power, source voltage, and reset operations report the previous and resulting state.
- On completion or failure, the library attempts to restore the session's starting power state and records whether restoration succeeded.
- No plugin hook or skill may automatically enable DUT power, change voltage, or reset a device.
- `ppk2lab web` is a viewer unless `--allow-control` is given, binds `127.0.0.1`, and refuses `--allow-control` on any other address. It can never reset the device, set a user gain, choose which device to open, or raise the session's voltage ceiling. Because the server holds the port for its whole lifetime, **closing the browser tab does not de-energise VOUT — only stopping the server does.**

## Use with AI agents

Every operation is available as a typed Python API and as a CLI command with stable JSON output. Read-only inspection is separated from operations that change hardware state.

Command surface:

```text
ppk2lab discover
ppk2lab info
ppk2lab capabilities
ppk2lab schema
ppk2lab configure
ppk2lab capture
ppk2lab inspect
ppk2lab decode
ppk2lab measure
ppk2lab assert
ppk2lab compare
ppk2lab export
ppk2lab doctor
```

Commands that enable DUT power, change source voltage, or reset hardware are explicitly identified as state-changing. Voltage arguments include their unit in the API name, such as `voltage_mv`.

An optional agent adapter may be provided as a separate package. The core driver and file formats do not depend on one agent framework.

Every JSON result carries a schema version, stable error code, remediation hint, completion state, and sample-gap information. `ppk2lab capabilities --json` describes the live command and decoder surface, and publishes the coded warning, gap-reason, and interruption-reason catalogs, so an agent does not need to infer unsupported behavior or parse prose to learn what a `W_*` code means.

### Bundled agent skills

The plugin ships two skills built on progressive disclosure instead of one oversized prompt: each `SKILL.md` is a thin router that keeps the safety contract inline and maps tasks to per-topic reference notes the agent reads only when needed. Live knowledge — commands, options, decoder rate tiers, schemas, error codes — is never duplicated into prompts; agents query the self-describing CLI (`capabilities --json`, `schema`, `doctor --json`) instead.

| Skill | Covers | On-demand references |
|---|---|---|
| `ppk2lab-operate` | Measuring with a PPK2: setup and troubleshooting, safe configuration and capture, long recordings, triggers, UART/SPI/logic decoding, energy analysis, CI power regression, hardware diagnostics | `setup`, `capture`, `triggers`, `decode`, `analysis`, `regression`, `diagnostics` |
| `ppk2lab-maintain` | Maintaining this repository: observation-only protocol research, release-gate auditing | `protocol-research`, `release-gating` |

Skills provide workflow guidance; the `ppk2lab` CLI remains the deterministic execution and validation layer. Installation never enables DUT power or starts a capture automatically.

## Architecture

```text
USB CDC transport
  -> device discovery and capability query
  -> streaming frame assembler
  -> raw sample parser and sequence-gap detector
  -> calibration and synchronized D0-D7 transitions
  -> trigger engine
  -> UART/SPI decoder plugins
  -> event-level charge and energy analysis
  -> capture files, CSV, VCD, JSONL, Python, and CLI
```

The canonical capture format preserves raw samples, calibration metadata, device and firmware identity, capture configuration, timestamps, and data-loss markers. Processed exports never replace the original evidence.

## Official references

PPK2 functionality is based on Nordic Semiconductor's official PPK2 documentation and the official [Power Profiler app repository](https://github.com/NordicSemiconductor/pc-nrfconnect-ppk). These official materials define the product behavior and compatibility reference for this project.

The project does not distribute Nordic firmware binaries. Firmware installation and updates should be performed with Nordic's official tools unless separate redistribution permission is established.

Nordic Semiconductor, Power Profiler Kit, and PPK2 may be trademarks or product names of Nordic Semiconductor ASA. Their use here is solely to identify compatible hardware.

See [docs/sources.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/sources.md) and [docs/protocol-spec.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/protocol-spec.md) for the Nordic official references and documented PPK2 behavior used by the project.

## Roadmap

`0.5.0` is released. What is still open is validation rather than implementation, and it is worth knowing which is which. One hardware session on 2026-08-20 ran the tool end to end on a single unit — firmware fingerprint `HW=49625 IA=59.0 keys=40 ports=2`, macOS on Apple silicon — covering discovery, metadata, calibrated capture at 100 kS/s, interruption recovery, and every offline command. These are **not** validated, and each needs hardware that session did not have:

- a physical pass on Windows and on Linux, where the OS reports the USB interface numbers macOS does not;
- a second unit and a second firmware fingerprint, only one of each having been seen;
- a cross-check that can resolve gain error: a resistor an order of magnitude tighter than ±5%, or a calibrated reference;
- UART and SPI decoder validation on real signals from an MCU fixture, against defined error-rate thresholds;
- hot-unplug recovery, an 8-24 h soak run, and a multi-device session — plus a bandwidth sweep and a load that crosses a current-range boundary;
- Claude Code and Codex skill installation verified against the current releases, and the documentation and examples reproduced from a clean environment.

The browser console `0.4.0` shipped without a server has one in `0.5.0`: `pip install 'ppk2lab[web]'` and `ppk2lab web`. It has not been run against a physical PPK2 either — every test behind it uses the simulator, which exercises the protocol and the state machine but not the instrument. [docs/webui.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/webui.md) says what it does, what it refuses to do, and what has not been proven.

The decoder support tiers are named for the rate each was designed against, not for a measured error rate — nothing has been measured yet. Validation that needs hardware has no CI workflow and is not meant to, so the maintainer runs it on the bench and fills in the compatibility matrix as units and platforms are covered. See [ROADMAP.md](https://github.com/tipoLi5890/ppk2lab/blob/main/ROADMAP.md) for that matrix and for what is deliberately not being built.

## Documentation

| Document | Covers |
|---|---|
| [INSTALL.md](https://github.com/tipoLi5890/ppk2lab/blob/main/INSTALL.md) | Python, USB permissions, Claude Code, Codex, CI, and troubleshooting |
| [ROADMAP.md](https://github.com/tipoLi5890/ppk2lab/blob/main/ROADMAP.md) | Milestones, support levels, and release exit criteria |
| [docs/SPEC.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/SPEC.md) | Public data model, states, API, and schema contracts |
| [docs/api-baseline.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/api-baseline.md) | Frozen public API surface and stability policy |
| [docs/cli-reference.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/cli-reference.md) | CLI commands, flags, JSON output, and exit codes |
| [docs/faq.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/faq.md) | Sample rate, energy, mode semantics, and other recurring questions |
| [docs/protocol-spec.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/protocol-spec.md) | Documented PPK2 protocol, commands, fields, and device behavior |
| [docs/capture-format.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/capture-format.md) | Canonical loss-aware capture artifact |
| [docs/decimation.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/decimation.md) | Decimated export buckets: what each one contains, and what it says it does not |
| [docs/calibration.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/calibration.md) | Raw-sample to current conversion formula and units |
| [docs/bandwidth.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/bandwidth.md) | Current-channel sampling model, aliasing, and which statistics survive it |
| [docs/logic-port.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/logic-port.md) | D0-D7 wiring, Logic VCC, levels, and usable bandwidth |
| [docs/decoders.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/decoders.md) | UART/SPI contracts, confidence, gaps, and physical limits |
| [docs/triggers.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/triggers.md) | Trigger types and pre/post-trigger capture windows |
| [docs/energy-analysis.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/energy-analysis.md) | Charge, energy, peak-current, and latency definitions |
| [docs/agent-interface.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/agent-interface.md) | JSON, tools, state-changing operations, and context budgets |
| [docs/sources.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/sources.md) | Nordic official documentation and repository references |
| [docs/webui.md](https://github.com/tipoLi5890/ppk2lab/blob/main/docs/webui.md) | The browser console: running it, what `--allow-control` does and does not allow, and the wire it speaks |
| [SECURITY.md](https://github.com/tipoLi5890/ppk2lab/blob/main/SECURITY.md) | Hardware, USB, file, and untrusted-input security model |

## Contact

Questions, bugs, compatibility reports, and feature requests should be filed through [GitHub Issues](https://github.com/tipoLi5890/ppk2lab/issues).

## License

Released under the [MIT License](https://github.com/tipoLi5890/ppk2lab/blob/main/LICENSE). It covers the original material in this repository only — PPK2 hardware, Nordic firmware, Nordic documentation, and trademarks remain with their owners.

## Development

This project is developed with assistance from Claude Code and OpenAI Codex.
