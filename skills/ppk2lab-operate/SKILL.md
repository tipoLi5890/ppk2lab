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
  `remediation`, and warnings are `{code, message, category}` too. Those
  three keys are the same live and in a stored manifest, so `inspect` and
  `capture` describe the same warning the same way — branch on the codes,
  never on message text
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
   through the meter (point 4 is why): say so, don't switch power on to
   "check".
4. **DUT power does not outlive the command that enabled it.** The
   instrument de-energizes VOUT once the USB host goes away. Measured on
   hardware, varying only how long the serial port stayed closed: still on
   in 3 of 3 trials at 0 ms, 1 of 3 at 100 ms, 2 of 3 at 250 ms, and 0 of 3
   at 500 ms and beyond. Deterministically off by half a second.

   So `configure --dut-power on --apply` cannot power a DUT for a later,
   separate `capture`. It exits 0, reports `applied: true`, and warns
   `W_DUT_POWER_TRANSIENT` — and the power is gone before the next process
   opens the port. **This failure is silent and its output looks fine:**
   the capture succeeds, reads near zero, and near zero is a plausible
   sleep current. A powered measurement has to happen inside one open
   session (`ppk2lab.PPK2` in Python), and the measured current is the only
   verification available, because this hardware cannot report its power
   state back.
5. Never pass `--overwrite` without the user confirming replacement.
6. `observed_after: false` means "requested, not confirmed by the device" —
   say so. DUT power has no readback at all, so it is always unconfirmed.
7. A session restores the power state it found when it closes, falling back
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
4. **A quantile can be a bound rather than a measurement.** The
   distribution grid is logarithmic and starts at 200 nA, so it cannot bin
   a reading at or below zero — which an unloaded input legitimately
   produces. Measured on an idle PPK2 over 60 s: 69-71% of samples fell
   below the floor and `p50` came back as exactly 200 nA, the floor
   itself. `distribution.quantiles_at_floor` names which quantiles are
   affected and `W_BELOW_MEASUREMENT_FLOOR` fires. Asked "what is the
   sleep current", answer with the mean, the minimum, or charge — never
   with a percentile that bottomed out.
5. **Loss is reported, and no longer expected.** Through `0.2.0` a 60 s
   capture on an idle macOS host lost 25,792 samples (0.43%) in exactly 5
   `host_overflow` gaps, and under load the gaps got *larger* rather than
   more numerous. Both facts were the signature of a bug in this project:
   the artifact writer compressed each 10-second chunk on the sample thread,
   so a 60 s capture lost one gap per chunk boundary and load only made each
   stall longer. Fixed in Unreleased; captures on that same host are now
   gap-free. Still read `covered_fraction` and `charge_is_lower_bound`
   before quoting an integral, still report loss rather than calling it a
   device fault — but a gappy capture is now worth investigating rather than
   shrugging at. `gap_reasons` in `capabilities --json` says where each loss
   happened.
6. **The coded warnings name which of those it was.** The ones that decide
   trust:
   - `W_SAMPLE_GAPS` — samples were lost; `sample_gaps` locates each one.
   - `W_BELOW_MEASUREMENT_FLOOR` — a reported quantile is served from the
     grid floor (point 4); it bounds the value from above.
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
7. **The wall clock is the only witness** to a loss of exactly k × 64
   samples, which the device's 6-bit counter cannot express.
   `timeline.rate_check: "deficit"` reports the coarse case (more than 10%
   short, over at least 2 s); `W_UNACCOUNTED_SAMPLES` reports the finer
   one. Either way, durations and integrals understate reality by more
   than the gap table shows. When the gap table already accounts for the
   loss the witness has nothing to add and `rate_check` stays `ok` — that
   is the expected result, not a missed detection.
8. **`energy_uj: null` is an answer, not an error.** The meter never
   measures the DUT's voltage. Never present energy without its
   `voltage_basis`.
9. **Exit code 6 means "cannot be evaluated"** — neither pass nor fail.
   Rerun the capture; never round it to success.
10. **Traceability**: quote `capture_id` and `capture_sha256` with every
    number, so a conclusion stays attached to the evidence it came from.
    On a windowed read `capture_sha256` is `null` by design
    (`W_PARTIAL_INTEGRITY`) — quote `capture_id` and say why.

## What has been checked on hardware

One physical session exists: one PPK2, one firmware fingerprint, macOS
only. It covered discovery, calibration, capture, loss accounting,
interruption and recovery, the artifact commands, and DUT-power lifetime —
every hardware figure in this skill and its references comes from it. The
handful of figures labelled "demo profile" come from `--simulate` and are
not measurements of anything.

It did **not** cover: any calibrated reference (the cross-check load was a
±5% resistor, which can show the absence of a gross error and nothing
finer), Windows or Linux, a second unit, UART or SPI on real signals (there
is no MCU fixture, so there is no measured decoder error rate), hot unplug,
multi-device sessions, a load at a range boundary, or any run longer than
60 s. When a conclusion rests on one of those, say that the project has not
measured it.

## Task → reference map

| The task involves | Read |
|---|---|
| installing, device not found, permissions, port busy | [references/setup.md](references/setup.md) |
| configuring the device and recording captures; long recordings; looking at an artifact before loading it | [references/capture.md](references/capture.md) |
| capturing on a condition (current/digital/protocol trigger, pre/post windows) | [references/triggers.md](references/triggers.md) |
| UART or SPI content, D0-D7 edges/pulses/timing, VCD export | [references/decode.md](references/decode.md) |
| current, charge, energy, percentiles, duty cycle, peak, latency numbers; differencing two captures | [references/analysis.md](references/analysis.md) |
| pass/fail power assertions, CI regression gates | [references/regression.md](references/regression.md) |
| readings look wrong: wiring, Logic VCC, flat/noisy channels | [references/diagnostics.md](references/diagnostics.md) |

A worked end-to-end agent flow lives in the repository at
`examples/agent_workflow.md` (not required reading for routine tasks).
