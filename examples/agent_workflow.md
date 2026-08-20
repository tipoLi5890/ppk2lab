# Agent workflow example

A tool-using agent (Claude Code, Codex, a CI script) should drive ppk2lab
through the CLI JSON contract. Every command returns the same envelope:

```json
{
  "schema_version": "1",
  "command": "...",
  "ok": true,
  "result": { },
  "warnings": [],
  "error": null
}
```

Every entry in `warnings` is `{code, message, category}`, and those three
keys are the same live and in a stored manifest — a warning `inspect` reads
back out of an artifact branches exactly like one `capture` returned.

All examples below work without hardware by adding `--simulate`.

## 1. Survey before touching anything (read-only)

```bash
ppk2lab doctor --json          # environment, permissions, devices, calibration
ppk2lab discover --json        # devices, serial numbers, measurement ports
ppk2lab capabilities --json    # live command surface, decoder tiers, error codes
ppk2lab info --device <serial> --json
```

Rules for the agent:

- `doctor` exits nonzero when a check fails, so `ppk2lab doctor --json || abort`
  is a working pre-flight gate. Apply each failing check's `remediation` before
  retrying; `warn` and `skip` do not block, and `--simulate doctor` exits 0.
- Never guess a voltage from the DUT's name. If the user has not provided
  `voltage_mv`, stop and ask.
- `capabilities` is generated from the live parser — trust it over any cached
  documentation. It also publishes three coded catalogs in one
  `{code, category, meaning}` shape: `warning_codes`, `gap_reasons`, and
  `interruption_reasons`. All three are open, so handle an unrecognized code by
  reporting it, not by dropping it.

## 2. Configure explicitly (state-changing, dry-run first)

```bash
ppk2lab configure --device <serial> --mode source --voltage-mv 3300 --json
# review the projected state, then apply deliberately:
ppk2lab configure --device <serial> --mode source --voltage-mv 3300 --apply --json
```

The result records `requested`, `before`, `after`, and whether the after
state was actually read back (`observed_after`). DUT power cannot be read
back from metadata; the result says so instead of pretending.

Nothing turns DUT power on for you: `configure` without `--apply` is a dry run,
`capture` never touches power, and `doctor --stream-check` only starts and
stops the sample stream. When the session closes, the library restores the
power state it found, falling back to OFF with a recorded warning if the
starting state was unknown.

### DUT power does not survive the command that enabled it

Mode and source voltage are metadata-backed and persist across processes.
VOUT does not: the instrument de-energizes it once the USB host goes away.
Measured by varying only how long the port stayed closed — still on in 3 of 3
trials at 0 ms, 1 of 3 at 100 ms, 2 of 3 at 250 ms, and 0 of 3 at 500 ms and
beyond. Deterministically off by half a second.

So `configure --dut-power on --apply` followed by a separate `capture` does
not produce a powered measurement, and it fails silently: `configure` exits 0
with `applied: true` and warns `W_DUT_POWER_TRANSIENT`, then the capture reads
near zero — which is exactly what a good sleep-current result looks like. A
powered measurement has to stay inside one open session:

```python
from ppk2lab import PPK2, Mode

with PPK2.open(serial_number="<serial>") as dev:   # or simulate=True
    dev.set_mode(Mode.SOURCE)
    dev.set_source_voltage_mv(3300)
    dev.set_dut_power(True)                        # only on explicit request
    result = dev.capture(duration_s=5.0, output="run.ppk2a")
print(result.stats.mean_ua, result.complete)
```

This hardware cannot report its power state back, so the measured current is
the only verification there is: a plausible non-zero draw is the evidence that
power was on, and nothing else is.

## 3. Capture (measurement; never enables DUT power)

```bash
ppk2lab capture --device <serial> --duration 5s --output run.ppk2a --json
```

Check `result.complete`. If false, the capture has gaps or was interrupted;
`result.stats.sample_gaps` lists every loss with its timeline index. Exit
code 6 signals an incomplete capture. Durations accept `us`, `ms`, `s`,
`min` and `h`, so a soak run is `--duration 8h`.

`--tag KEY=VALUE` (repeatable) records what the run is *of* — board serial,
firmware build, experiment id — inside the artifact. The capture result hands
them straight back as `result.user_tags`, so a script that just tagged a run
does not reopen the file to read them, and `inspect --json` returns the same
map later. Nothing interprets a tag; values stay the strings that went in.

Loss is always reported, and since the `0.3.0` fix it is no longer the
normal outcome: through `0.2.0` a 60 s capture lost 5 `host_overflow` gaps
because the artifact writer compressed each 10-second chunk on the sample
thread, and captures on that same host are now gap-free. Gaps are marked,
counted and located; report them instead of treating them as a device fault,
and check `gap_reasons` in `capabilities --json` for where a loss happened.

Triggered capture:

```bash
ppk2lab capture --device <serial> --trigger 'uart D0 9600 "BOOT"' \
  --pre 100ms --post 1s --trigger-timeout 30s --output boot.ppk2a --json
```

A UART content trigger cannot fire before the decoder has seen a confirmed
idle run, so it will not match whatever was mid-transmission when the capture
started, and it will not match a pattern stitched around a corrupted frame.

## 4. Look at an artifact before loading it

```bash
ppk2lab inspect run.ppk2a --json
```

`inspect` reads the manifest and never touches a sample chunk, so it works on
an artifact of any length. Every other offline command materializes samples and
is capped by `--max-samples` (default ~25 M, about 250 s at 100 kS/s). A longer
capture is not corrupt: it exits 2 with `CAPTURE_TOO_LARGE`, and the
remediation names the three ways forward — `inspect`, a window, or a raised
ceiling.

```bash
ppk2lab measure soak.ppk2a --window 3600:3660 --json
ppk2lab export soak.ppk2a --format csv --output slice.csv --window 3600:3660
```

A window reads only the chunks it falls in. In exchange, `capture_sha256` comes
back `null` with `W_PARTIAL_INTEGRITY` — only the chunks touched were verified
— and a window whose start falls inside a gap is refused rather than moved.

## 5. Decode and measure offline

```bash
ppk2lab decode run.ppk2a --uart D0 --baud 9600 --output uart.jsonl --json
ppk2lab measure run.ppk2a --annotations uart.jsonl --group-by frame --json
ppk2lab measure run.ppk2a --state-threshold 1mA --json
```

Annotations reference exact sample ranges (`start_sample`, `end_sample`). A
non-empty `errors` list always means `confidence: 0.0`, and such an annotation
is evidence of what the line did, never decoded data. Filter on `errors`:

- `gap` — samples are missing. Every gap now emits its own `error` annotation
  (`fields.reason: "sample_gap"`), even when no frame was in flight.
- `unsynced` — the frame was decoded before a confirmed idle run, so where
  frames start is unknown. It never joins the byte stream, anchors an
  assertion, or fires a trigger.

## 6. Judge whether a number is trustworthy

Before reporting any figure, read these fields:

| Field | Means |
|---|---|
| `complete` | gap-free **and** populated; a window past the capture's data is `false` |
| `charge_is_lower_bound` | samples were excluded — gap, unpopulated span, clipping, or a wall-clock deficit |
| `covered_fraction` | how much of the requested window actually holds samples |
| `distribution.quantiles_at_floor` / `W_BELOW_MEASUREMENT_FLOOR` | the named percentiles came from the 200 nA grid floor; they bound the value, they do not measure it |
| `saturated_samples` / `W_CLIPPED` | the reading was pinned at full scale, not measured; `max` is a floor |
| `W_UNACCOUNTED_SAMPLES` | wall time witnesses loss the gap table cannot localize |
| `timeline.rate_check` | `deficit` means loss beyond what the 6-bit counter can express |
| `voltage_basis` / `energy_uj: null` | the meter never measures the DUT's voltage |

Asked "what is the sleep current" on a near-idle DUT, answer with the mean,
the minimum, or charge. Measured on hardware with nothing drawing current,
69-71% of samples over 60 s fell below the grid floor and `p50` came back as
exactly 200 nA — the floor, not a reading. `assert` compares the number
without that flag attached, so check `measure --json` before writing a
percentile rule at that level.

## 7. Assert for regression testing

```bash
ppk2lab assert run.ppk2a \
  --rule 'after uart("TX_DONE"), within 20ms, avg_current < 10uA' --json
```

Interpretation:

| exit code | meaning |
|---|---|
| 0 | every rule passed |
| 1 | a rule failed, or its anchor event was not found (`no_event`) |
| 6 | at least one window could not be evaluated as written (neither pass nor fail) |

An `incomplete` outcome names why in each observation's `reason_code`:
`sample_gaps`, `window_past_capture_end`, `window_unpopulated`,
`metric_not_computable`, or `metric_at_measurement_floor` — the last when
the rule's own metric was served from the 200 nA grid floor and the verdict
would flip for any smaller true value. The window is evaluated as the rule
wrote it and is never trimmed to fit the data, so `within 20ms` against a
capture that ends 5 ms later is `incomplete`, not `passed`. Every observation
also carries `covered_fraction` and `capture_end_sample`.

An anchor is not matched across a sample gap, an unsynchronized or errored
UART frame, or two SPI CS transactions — a pattern stitched across a
discontinuity is not evidence the event happened. Prefer `p99_current` to
`max_current` for a burst ceiling: `max` is one sample and drifts upward with
capture length, a percentile does not.

For "how much did this change cost", difference the two captures rather than
quoting two absolute numbers:

```bash
ppk2lab compare baseline.ppk2a candidate.ppk2a --metric mean_current --json
```

The +/-10% per-range figure is a gain error, so it cancels across two captures
taken in one range on one instrument and the delta gets a much tighter bar
than either reading. Check that the premise held before quoting it:
`same_instrument` is `false` with `W_INSTRUMENT_MISMATCH` when the two serial
numbers differ and `null` when the captures did not identify their
instruments; an `energy` comparison publishes a `voltage` block and warns
`W_VOLTAGE_ASSUMED` when the two sides ran at different supplies. Both sides'
`measure` diagnostics come back as warnings tagged with their path, so a delta
between two floor-served quantiles is visible as the non-result it is.

Persist `capture_sha256` together with the verdict so conclusions stay
traceable to raw evidence.

## 8. Recovering from errors

Every error carries a stable `code` and a `remediation` string:

```json
{"code": "PORT_BUSY", "message": "...", "remediation": "Another process ...", "exit_code": 4}
```

The agent should act on `remediation`, not on parsing the human message.

## 9. Say what has not been measured

The hardware figures above come from one session: one PPK2, one firmware
fingerprint, macOS. Not covered by it — a calibrated reference (the one
known-load check used a ±5% resistor, which rules out a gross error and
nothing finer), Windows or Linux, a second unit, UART or SPI on real signals
(there is no MCU fixture, so no decoder error rate has been measured), hot
unplug, multi-device sessions, a load at a range boundary, and any run longer
than 60 s. When a conclusion depends on one of those, report it as unmeasured
rather than assumed.
