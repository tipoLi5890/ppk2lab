---
name: ppk2lab-operate
description: Control, capture, decode, and analyze Nordic PPK2 power and D0-D7 logic measurements through the ppk2lab CLI — setup and troubleshooting, safe configuration, captures and triggers, UART/SPI decoding, energy analysis, CI power assertions, and hardware diagnostics.
---

# Operating PPK2 hardware with ppk2lab

This skill is a router: it keeps the safety contract in front of you and
points to one reference per task. Read only the reference the current task
needs — everything else stays out of context.

## Explore, don't memorize

The CLI is self-describing. Prefer querying it over any static text
(including these references) whenever they could disagree:

- `ppk2lab capabilities --json` — live commands and options, state-changing
  classification, decoder rate tiers, error catalog, exit codes, schema list
- `ppk2lab schema --list` / `ppk2lab schema <name>` — every JSON contract
- `ppk2lab doctor --json` — environment and device diagnosis; each failing
  check carries its own `remediation` string — act on it, don't guess
- Every command supports `--json`; errors carry a stable `code` +
  `remediation`; branch on those, never on message text
- `--simulate` runs every command against a built-in simulated PPK2 for
  testing workflows — never report simulated output as a measurement

## Safety contract (always applies — not optional reading)

1. Never enable DUT power, change source voltage or mode, or reset the
   device unless the user asked for it in this task. `configure` is a dry
   run without `--apply`; show the dry run before applying.
2. Never infer a voltage from a DUT's name. Voltage is explicit millivolts
   (`--voltage-mv`); if the user has not provided it, stop and ask.
3. `capture` never touches DUT power. Never pass `--overwrite` without the
   user confirming replacement.
4. Check `complete` and `sample_gaps` in every result. Exit code 6 means
   data is missing — neither pass nor fail; never present it as success.
5. Keep `capture_id`/`capture_sha256` with every reported number so
   conclusions stay traceable to raw evidence.
6. `observed_after: false` in a state-change result means "requested, not
   confirmed by the device" — say so.

## Task → reference map

| The task involves | Read |
|---|---|
| installing, device not found, permissions, port busy | [references/setup.md](references/setup.md) |
| configuring the device and recording captures; long recordings | [references/capture.md](references/capture.md) |
| capturing on a condition (current/digital/protocol trigger, pre/post windows) | [references/triggers.md](references/triggers.md) |
| UART or SPI content, D0-D7 edges/pulses/timing, VCD export | [references/decode.md](references/decode.md) |
| current, charge, energy, peak, latency numbers | [references/analysis.md](references/analysis.md) |
| pass/fail power assertions, CI regression gates | [references/regression.md](references/regression.md) |
| readings look wrong: wiring, Logic VCC, flat/noisy channels | [references/diagnostics.md](references/diagnostics.md) |

A worked end-to-end agent flow lives in the repository at
`examples/agent_workflow.md` (not required reading for routine tasks).
