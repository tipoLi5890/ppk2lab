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

- `ppk2lab capabilities --json` — live commands and options (every option
  that constrains its values publishes `choices`), state-changing
  classification, decoder rate tiers and sync policy, exit codes, schema
  list, and three coded catalogs in one `{code, category, meaning}` shape:
  `warning_codes`, `gap_reasons`, `interruption_reasons`. All three are
  **open** — a code you do not recognize is a finding to relay, not noise.
- `ppk2lab schema --list` / `ppk2lab schema <name>` — every JSON contract
- `ppk2lab doctor --json` — environment and device diagnosis; it **exits
  nonzero when a check fails**, so `ppk2lab doctor --json || abort` is a
  usable pre-flight gate. Each failing check carries its own `remediation`
  string and the `exit_code` it maps to — act on it, don't guess
- Every command supports `--json`; errors carry a stable `code` +
  `remediation`, and warnings are `{code, message}` too — branch on the
  codes, never on message text
- `--simulate` runs every command against a built-in simulated PPK2 for
  testing workflows — never report simulated output as a measurement

## Safety contract (always applies — not optional reading)

1. Never enable DUT power, change source voltage or mode, or reset the
   device unless the user asked for it in this task. `configure` is a dry
   run without `--apply`; show the dry run before applying.
2. Never infer a voltage from a DUT's name. Voltage is explicit millivolts
   (`--voltage-mv`); if the user has not provided it, stop and ask. For a
   fragile DUT, set a hard ceiling with `--max-voltage-mv` (or
   `PPK2LAB_MAX_VOLTAGE_MV`): anything above it is refused before a byte
   reaches the device.
3. `capture` never touches DUT power, and neither does
   `doctor --stream-check` — both only start and stop the sample stream.
   A capture that reads near zero usually means the DUT was never powered
   through the meter: say so, don't switch power on to "check".
4. Never pass `--overwrite` without the user confirming replacement.
5. `observed_after: false` means "requested, not confirmed by the device" —
   say so. DUT power has no readback at all, so it is always unconfirmed.
6. A session restores the power state it found when it closes, falling back
   to OFF with a recorded warning if the starting state was unknown. Relay
   those restoration warnings.

## Is this measurement trustworthy?

The question you will actually be asked. Every part of the answer is a
field, not a judgement call — work down the list before quoting a number.

1. **`complete`** means gap-free **and** populated. A window that reaches
   past where the capture has data is `complete: false` even with no gap
   in it.
2. **`charge_is_lower_bound: true`** means samples were left out of the
   integral — a gap, an unpopulated span, a clipped reading, or a
   wall-clock deficit. Charge, energy, and anything derived from them
   understate reality; never present them as exact.
3. **`covered_fraction`** is how much of the window you asked about
   actually holds samples. 0.067 means you are describing 6.7% of the
   question.
4. **The coded warnings name which of those it was.** The ones that decide
   trust:
   - `W_SAMPLE_GAPS` — samples were lost; `sample_gaps` locates each one.
   - `W_WINDOW_UNPOPULATED` — the window extends past the capture's data.
   - `W_CLIPPED` — samples sat on the ADC's full-scale code. The amplitude
     was **pinned, not measured**: the peak is a floor and a flat top is an
     artifact of the front end, not a regulated load. `saturated_samples`
     and `saturated_ranges` say how much and where.
   - `W_UNACCOUNTED_SAMPLES` — elapsed wall time accounts for more samples
     than the timeline does. The loss is real but belongs to the whole
     capture; nothing localizes it, so `covered_fraction` cannot show it.
   - `W_GAP_TABLE_TRUNCATED` — the gap *count* is still exact, but the
     table does not list them all, so not every loss can be placed in time.
   - `W_PARTIAL_INTEGRITY` — a windowed read verified only the chunks it
     touched, so `capture_sha256` is `null` instead of claimed.
   - `W_MANIFEST_IMPLAUSIBLE` — a stored manifest states something the
     capture cannot support. It is reported as stored, not corrected.
5. **The wall clock is the only witness** to a loss of exactly k × 64
   samples, which the device's 6-bit counter cannot express.
   `timeline.rate_check: "deficit"` reports the coarse case (more than 10%
   short, over at least 2 s); `W_UNACCOUNTED_SAMPLES` reports the finer
   one. Either way, durations and integrals understate reality by more
   than the gap table shows.
6. **`energy_uj: null` is an answer, not an error.** The meter never
   measures the DUT's voltage. Never present energy without its
   `voltage_basis`.
7. **Exit code 6 means "cannot be evaluated"** — neither pass nor fail.
   Rerun the capture; never round it to success.
8. **Traceability**: quote `capture_id` and `capture_sha256` with every
   number, so a conclusion stays attached to the evidence it came from.

## Task → reference map

| The task involves | Read |
|---|---|
| installing, device not found, permissions, port busy | [references/setup.md](references/setup.md) |
| configuring the device and recording captures; long recordings; looking at an artifact before loading it | [references/capture.md](references/capture.md) |
| capturing on a condition (current/digital/protocol trigger, pre/post windows) | [references/triggers.md](references/triggers.md) |
| UART or SPI content, D0-D7 edges/pulses/timing, VCD export | [references/decode.md](references/decode.md) |
| current, charge, energy, percentiles, duty cycle, peak, latency numbers | [references/analysis.md](references/analysis.md) |
| pass/fail power assertions, CI regression gates | [references/regression.md](references/regression.md) |
| readings look wrong: wiring, Logic VCC, flat/noisy channels | [references/diagnostics.md](references/diagnostics.md) |

A worked end-to-end agent flow lives in the repository at
`examples/agent_workflow.md` (not required reading for routine tasks).
