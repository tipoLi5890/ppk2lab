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
  `{code, message}` with a `W_*` code (`W_SAMPLE_GAPS`,
  `W_TIMELINE_COMPRESSION`, `W_VOLTAGE_ASSUMED`, `W_NOT_CALIBRATED`, …). A
  code's meaning never changes within a schema version, but codes are added,
  so the set is open. `capabilities --json` publishes the catalog with each
  code's meaning, which makes the vocabulary discoverable rather than
  something to memorize or pin.
- `ppk2lab capabilities --json` is generated from the live parser and
  registries: commands with their options, `choices`, and state-changing
  classification, decoder rate tiers, exit codes, the error, warning,
  gap-reason and interruption-reason catalogs, and the schema list. Prefer it
  over cached documentation.
- `ppk2lab schema <name>` serves every published JSON schema.

## Building a call without guessing

`capabilities --json` is sufficient on its own: every command carries a
`description`, and every option that constrains its values publishes
`choices`. An agent reads `commands[] → options[] → {flags, help, required,
choices}` and needs no other source. `--json` and `--simulate` are listed
once under `global_options`, not repeated per command, and are accepted
before or after the subcommand.

For example, `export` publishes `--format` with
`choices: ["csv", "vcd", "jsonl"]`, `--output` as required, and `--decimate`
with its help text, which is everything needed for:

```bash
ppk2lab --json export run.ppk2a --format csv --output summary.csv --decimate 1000
```

`choices` are stringified so the list has one type (`--spi-mode` is
`["0","1","2","3"]`). An option with no `choices` takes free-form values;
never infer an enum from prose.

## Coded catalogs to branch on

Three catalogs share one shape — `{code, category, meaning}` — so one
reader handles all of them:

| catalog | appears in | example |
|---|---|---|
| `warning_codes` | `warnings[].code` on every envelope | `W_SAMPLE_GAPS` |
| `gap_reasons` | `sample_gaps[].reason`, capture `gaps[].reason` | `counter_skip` |
| `interruption_reasons` | `interruption.reason` | `stream_stalled` |

All three are **open**. A new reason is a new way the hardware or host can
lose samples, not a breaking change: treat an unfamiliar code as unknown, not
as invalid, and never fail closed on one. `category` says where the problem
lives (`capture integrity`, `measurement trust`, `device state`, `analysis`;
for gaps, where the loss happened). It is deliberately **not** a severity —
how much a warning matters depends on the question being asked, which the
tool does not know.

Assertion observations carry their own `reason_code` when a window could not
be evaluated: `sample_gaps`, `window_past_capture_end`, `window_unpopulated`,
`metric_not_computable`, `metric_at_measurement_floor`. Branch on it rather
than on the prose `reason`.

An observation also carries `metric_is_upper_bound: true` when its metric is a
quantile the distribution grid could only bound from above. The verdict is
still reported when it holds for every smaller true value — `p99 < 1mA`
passing, or `p99 > 1mA` failing — and becomes `incomplete` when it would flip.

## Read-only vs state-changing

| Read-only (safe to run any time) | State-changing / measurement |
|---|---|
| `discover`, `info`, `capabilities`, `schema`, `doctor` (default), offline `inspect`/`decode`/`measure`/`assert`/`export` | `configure` (requires `--apply`), `capture` (starts/stops measurement; never touches DUT power), `doctor --stream-check` (measurement only) |

`doctor` exits nonzero when a check fails, so `ppk2lab doctor --json || stop`
is a working pre-flight gate; `warn` and `skip` never block. Use it before a
bench session rather than discovering the problem mid-capture.

Hardware-safety rules an agent must follow (the trust rules are the checklist
further down):

1. Never enable DUT power, change voltage, or reset unless the user asked
   for it in this task. Use `configure` without `--apply` first and show the
   projected change.
2. Never infer a voltage from a DUT name. If `voltage_mv` was not supplied
   by the user or a fixture profile, stop and ask.
3. Treat `observed_after: false` in a StateChange as "requested, not
   confirmed".
4. **DUT power does not survive the command that enabled it.** The device
   de-energizes VOUT once the host closes the serial port — measured off in
   every trial once the port had been closed for 500 ms. So
   `configure --dut-power on --apply` followed by a separate `capture` measures
   an *unpowered* board, whatever `applied: true` said. The configure result
   carries `W_DUT_POWER_TRANSIENT` for exactly this. A powered measurement has
   to happen inside one open session (`ppk2lab.PPK2` in Python), and has to be
   confirmed from the current itself, because this hardware cannot report its
   power state back.

## Fields whose meaning an agent must not soften

- **`complete`** on a window means gap-free **and** populated end to end. A
  10-second window over a 50 ms capture is *not* complete: it contains no gap
  because it contains almost nothing. `samples.unpopulated` counts the
  positions that hold no data and `W_WINDOW_UNPOPULATED` says so. Do not
  report statistics from such a window as an answer about the window that
  was asked for.
- **`charge_is_lower_bound`** covers every sample that happened and was not
  counted, not only gaps: an invalid range field, missing calibration, a
  current beyond what the hardware can carry, a sample pinned on the ADC's
  full-scale code, a stretch of window the capture never reached, and loss
  the wall clock witnessed but the counter could not describe. When it is
  true, `charge_uc` and `energy_uj` are floors. Never present a floor as a
  total.
- **`voltage_basis`** is one of `caller_override`, `configured_source`,
  `device_metadata`, `unknown`. It names where the voltage behind an energy
  figure came from; `voltage_measured` is always `false`, because the PPK2
  measures current only. An energy number without its basis is not a result.
- **`distribution.quantiles_at_floor`** names any reported quantile that was
  served from the distribution grid's 200 nA floor rather than measured. Such a
  value is an **upper bound**: the true quantile is at or below it. The result
  carries `W_BELOW_MEASUREMENT_FLOOR` and human output prints `<=`. An empty
  list is the only thing that makes `p50`/`p90`/`p99`/`p999` a measurement.
  Expect a non-empty list on any lightly loaded input — an unloaded PPK2
  measured over 60 s read below the floor 69-71% of the time, so its `p50` is
  the floor and nothing else. Fall back to `current_ua.mean`,
  `current_ua.min`, or `charge_uc`, which are computed from samples rather
  than from the grid.
- **`capture_sha256`** is `null` for a windowed read (`measure --window`,
  `export --window`), with a `W_PARTIAL_INTEGRITY` warning: only the chunks
  the window touched were checked. Record the `null` and the warning; do not
  substitute the window's own digest, which answers a different question.
- **`uncertainty`** fields are all named `..._typical...` and `guaranteed` is
  always `false`. They are Nordic's typical figures, not limits, and
  `mean_ua_typical` (systematic) and `mean_ua_batch_stderr` (statistical) are
  different quantities that must never be added — docs/SPEC.md.

## Deciding whether a measurement can be trusted

The question an agent is actually asked is not "what was the current" but
"may I report this". Every input to that decision is a field, and none of them
requires reading prose:

1. **Did the command succeed?** `ok`, and on failure `error.code` /
   `error.exit_code`. Exit 6 is neither pass nor fail — data is missing — and
   an incomplete result is never presented as a success.
2. **Is the window the one that was asked for?** `complete` is true only
   when the window is gap-free *and* populated. Otherwise read `sample_gaps`,
   `samples.covered_fraction`, `samples.missing_known` and
   `samples.unpopulated` for how much of it is real.
3. **Are the integrals totals or floors?** `charge_is_lower_bound`. When true,
   `charge_uc` and `energy_uj` are floors, whatever caused it — a gap, an
   excluded sample, saturation, or a window reaching past the capture.
4. **Is each reported number a measurement?** A quantile counts as one only
   when `distribution.quantiles_at_floor` is empty, and `current_ua.max`
   counts as one only when `saturated_samples` is zero — a pinned ADC code is
   a ceiling, not a reading.
5. **Where did the voltage come from?** `voltage_basis`, with
   `voltage_measured` always `false`. An energy figure without its basis is
   not a result. `energy_uj: null` on an ampere-mode capture is the correct
   answer rather than a failure — pass the DUT's real supply voltage with
   `--assume-voltage-mv` if energy is wanted.
6. **How big is the error bar, and of what kind?** `uncertainty.mean_ua_typical`
   is systematic and `uncertainty.mean_ua_batch_stderr` is statistical; they
   are different quantities and are never added. `uncertainty.guaranteed` is
   always `false`.
7. **Was anything lost that the gap table cannot see?** On a capture,
   `timeline.rate_check`. The device's 6-bit counter cannot describe a loss of
   64 samples or more, so each capture is also cross-checked against the wall
   clock, and `deficit` means samples went missing however quiet the gap table
   is. Note what `ok` does *not* mean: on real 60 s captures that lost 0.43%
   to 5.1% of their samples to host overflow, `rate_check` stayed `ok`,
   correctly — the gap table had already accounted for that loss. It is not a
   claim that a capture is gap-free; `complete` and `sample_gaps` are.
8. **Was the whole artifact verified?** `capture_sha256`, which is `null` with
   `W_PARTIAL_INTEGRITY` after a windowed read.
9. **Did the run finish?** `interruption`, whose `reason` comes from the
   `interruption_reasons` catalog. `keyboard_interrupt` also covers a SIGTERM
   from a process manager, so it is not evidence that a human intervened.

Then read every `warnings[].code` against the catalog. A code you do not
recognize is unknown, not invalid — and not a reason to fail closed.

Persist `capture_id` and `capture_sha256` with whatever you conclude, so the
conclusion can be audited back to the raw evidence.

## Context budgets

Captures are large (400 kB/s of raw data). Agents should keep samples out of
the model context:

- `capture --output file.ppk2a --json` returns a summary (stats, gaps,
  hashes) — a few hundred bytes — plus an artifact handle (the path).
- `measure`, `assert`, and `decode --output annotations.jsonl` return
  aggregates and file handles, not sample dumps.
- Only `decode` without `--output` inlines annotations; prefer `--output`
  for anything longer than a screenful.
- `inspect CAPTURE.ppk2a` reads the manifest and opens no sample chunk, so
  its cost is independent of capture length. It is the right first call
  against an unfamiliar or hours-long artifact; the offline commands
  materialize the samples and refuse past `--max-samples`
  (`CAPTURE_TOO_LARGE`, exit 2 — an intact file that is simply large, not a
  damaged one).
- `measure --window` and `export --window` read only the chunks the window
  falls in, which is what bounds the *read*. `export --decimate N` /
  `--bucket-ms MS` writes one record per timeline bucket instead of one per
  sample, which bounds the *output* — the capture is still materialized, so
  `--max-samples` still applies. Combine the two for a long artifact.

## Simulated device

`--simulate` (or `PPK2LAB_SIMULATE=1`) provides a deterministic simulated
PPK2 with a repeating activity cycle (12 mA burst, one 5 kHz SPI
transaction on D3-D6, a 9600-baud UART `TX_DONE\n` on D0, 1 kHz square on
D2, 6 uA sleep). Every documented command works against it. Use it to test
workflows and CI plumbing; never report simulated numbers as measurements.

## MCP

The CLI and Python API are the single source of logic for 0.2.0. An optional
local stdio MCP server is planned only after the core API freezes
(`ROADMAP.md`); its tools will validate arguments, classify state-changing
operations (explicit `acknowledge_state_change`), and call this same
library rather than duplicating logic.
