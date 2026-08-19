# Agent interface: JSON contracts, tool safety, and context budgets

ppk2lab is designed so a tool-using agent never needs to scrape a GUI or
guess device state. This document defines how agents should call it. A
worked example lives in `examples/agent_workflow.md`.

## Contract summary

- One action = one CLI command (or one Python call). Every command supports
  `--json` and returns the `envelope` schema with `schema_version`, `ok`,
  `result`, `warnings`, `error`.
- Errors carry a stable `code`, a `remediation` string to act on, and a
  documented `exit_code`. Agents branch on `code`/`exit_code`, never on
  message text.
- Warnings are the same shape: every entry in `warnings` is
  `{code, message}` with a frozen `W_*` code (`W_SAMPLE_GAPS`,
  `W_TIMELINE_COMPRESSION`, `W_VOLTAGE_ASSUMED`, `W_NOT_CALIBRATED`, …).
  `capabilities --json` publishes the catalog with each code's meaning, so
  the vocabulary is discoverable rather than something to memorize.
- `ppk2lab capabilities --json` is generated from the live parser and
  registries: commands with their options and state-changing classification,
  decoder rate tiers, exit codes, error and warning catalogs, schema list.
  Prefer it over cached documentation.
- `ppk2lab schema <name>` serves every published JSON schema.

## Read-only vs state-changing

| Read-only (safe to run any time) | State-changing / measurement |
|---|---|
| `discover`, `info`, `capabilities`, `schema`, `doctor` (default), offline `decode`/`measure`/`assert`/`export` | `configure` (requires `--apply`), `capture` (starts/stops measurement; never touches DUT power), `doctor --stream-check` (measurement only) |

Safety rules an agent must follow:

1. Never enable DUT power, change voltage, or reset unless the user asked
   for it in this task. Use `configure` without `--apply` first and show the
   projected change.
2. Never infer a voltage from a DUT name. If `voltage_mv` was not supplied
   by the user or a fixture profile, stop and ask.
3. Treat `observed_after: false` in a StateChange as "requested, not
   confirmed".
4. Check `complete` and `sample_gaps` in every capture/measure/assert
   result. Never present an incomplete result as a success; exit code 6
   means "neither pass nor fail — data is missing".
5. Check `timeline.rate_check` on captures. The device's 6-bit counter
   cannot describe a loss of 64 samples or more, so each capture is
   cross-checked against the wall clock; `deficit` means samples went
   missing however quiet the gap table is.
6. Energy carries its provenance: `voltage_basis` and
   `voltage_measured: false`, because the instrument measures current only.
   `energy_uj: null` in ampere mode is the correct answer, not a failure —
   supply the DUT's real voltage with `--assume-voltage-mv` to compute it.
7. Persist `capture_id` and `capture_sha256` with every conclusion so it can
   be audited against raw evidence.

## Context budgets

Captures are large (400 kB/s of raw data). Agents should keep samples out of
the model context:

- `capture --output file.ppk2a --json` returns a summary (stats, gaps,
  hashes) — a few hundred bytes — plus an artifact handle (the path).
- `measure`, `assert`, and `decode --output annotations.jsonl` return
  aggregates and file handles, not sample dumps.
- Only `decode` without `--output` inlines annotations; prefer `--output`
  for anything longer than a screenful.

## Simulated device

`--simulate` (or `PPK2LAB_SIMULATE=1`) provides a deterministic simulated
PPK2 with a repeating activity cycle (12 mA burst, one 5 kHz SPI
transaction on D3-D6, a 9600-baud UART `TX_DONE\n` on D0, 1 kHz square on
D2, 6 uA sleep). Every documented command works against it. Use it to test
workflows and CI plumbing; never report simulated numbers as measurements.

## MCP

The CLI and Python API are the single source of logic for 0.1.0. An optional
local stdio MCP server is planned only after the core API freezes
(`ROADMAP.md`); its tools will validate arguments, classify state-changing
operations (explicit `acknowledge_state_change`), and call this same
library rather than duplicating logic.
