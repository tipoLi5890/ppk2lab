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
# review the projected state, then apply deliberately. --dut-power on belongs
# here only when the user asked for it; nothing enables it for you:
ppk2lab configure --device <serial> --mode source --voltage-mv 3300 --dut-power on --apply --json
```

The result records `requested`, `before`, `after`, and whether the after
state was actually read back (`observed_after`). DUT power cannot be read
back from metadata; the result says so instead of pretending.

`--dut-power on` belongs in the command line only when the user asked for it.
Nothing turns DUT power on for you: `configure` without `--apply` is a dry run,
`capture` never touches power, and `doctor --stream-check` only starts and
stops the sample stream. When the session closes, the library restores the
power state it found, falling back to OFF with a recorded warning if the
starting state was unknown.

## 3. Capture (measurement; never enables DUT power)

```bash
ppk2lab capture --device <serial> --duration 5s --output run.ppk2a --json
```

Check `result.complete`. If false, the capture has gaps or was interrupted;
`result.stats.sample_gaps` lists every loss with its timeline index. Exit
code 6 signals an incomplete capture. Durations accept `us`, `ms`, `s`,
`min` and `h`, so a soak run is `--duration 8h`.

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
| `saturated_samples` / `W_CLIPPED` | the reading was pinned at full scale, not measured; `max` is a floor |
| `W_UNACCOUNTED_SAMPLES` | wall time witnesses loss the gap table cannot localize |
| `timeline.rate_check` | `deficit` means loss beyond what the 6-bit counter can express |
| `voltage_basis` / `energy_uj: null` | the meter never measures the DUT's voltage |

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
`sample_gaps`, `window_past_capture_end`, `window_unpopulated`, or
`metric_not_computable`. The window is evaluated as the rule wrote it and is
never trimmed to fit the data, so `within 20ms` against a capture that ends
5 ms later is `incomplete`, not `passed`. Every observation also carries
`covered_fraction` and `capture_end_sample`.

An anchor is not matched across a sample gap, an unsynchronized or errored
UART frame, or two SPI CS transactions — a pattern stitched across a
discontinuity is not evidence the event happened. Prefer `p99_current` to
`max_current` for a burst ceiling: `max` is one sample and drifts upward with
capture length, a percentile does not.

Persist `capture_sha256` together with the verdict so conclusions stay
traceable to raw evidence.

## 8. Recovering from errors

Every error carries a stable `code` and a `remediation` string:

```json
{"code": "PORT_BUSY", "message": "...", "remediation": "Another process ...", "exit_code": 4}
```

The agent should act on `remediation`, not on parsing the human message.
