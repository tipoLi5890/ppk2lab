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

## 1. Inspect before touching anything (read-only)

```bash
ppk2lab doctor --json          # environment, permissions, devices, calibration
ppk2lab discover --json        # devices, serial numbers, measurement ports
ppk2lab capabilities --json    # live command surface, decoder tiers, error codes
ppk2lab info --device <serial> --json
```

Rules for the agent:

- If `doctor` reports a failing check, apply its `remediation` before retrying.
- Never guess a voltage from the DUT's name. If the user has not provided
  `voltage_mv`, stop and ask.
- `capabilities` is generated from the live parser — trust it over any cached
  documentation.

## 2. Configure explicitly (state-changing, dry-run first)

```bash
ppk2lab configure --device <serial> --mode source --voltage-mv 3300 --json
# review the projected state, then apply deliberately:
ppk2lab configure --device <serial> --mode source --voltage-mv 3300 --dut-power on --apply --json
```

The result records `requested`, `before`, `after`, and whether the after
state was actually read back (`observed_after`). DUT power cannot be read
back from metadata; the result says so instead of pretending.

## 3. Capture (measurement; never enables DUT power)

```bash
ppk2lab capture --device <serial> --duration 5s --output run.ppk2a --json
```

Check `result.complete`. If false, the capture has gaps or was interrupted;
`result.stats.sample_gaps` lists every loss with its timeline index. Exit
code 6 signals an incomplete capture.

Triggered capture:

```bash
ppk2lab capture --device <serial> --trigger 'uart D0 9600 "BOOT"' \
  --pre 100ms --post 1s --trigger-timeout 30s --output boot.ppk2a --json
```

## 4. Decode and measure offline

```bash
ppk2lab decode run.ppk2a --uart D0 --baud 9600 --output uart.jsonl --json
ppk2lab measure run.ppk2a --annotations uart.jsonl --group-by frame --json
```

Annotations reference exact sample ranges (`start_sample`, `end_sample`).
Annotations with a `gap` error and confidence 0 must never be treated as
decoded data.

## 5. Assert for regression testing

```bash
ppk2lab assert run.ppk2a \
  --rule 'after uart("TX_DONE"), within 20ms, avg_current < 10uA' --json
```

Interpretation:

| exit code | meaning |
|---|---|
| 0 | every rule passed |
| 1 | a rule failed or its event was not found |
| 6 | an evaluation window overlaps missing data (neither pass nor fail) |

Persist `capture_sha256` together with the verdict so conclusions stay
traceable to raw evidence.

## 6. Recovering from errors

Every error carries a stable `code` and a `remediation` string:

```json
{"code": "PORT_BUSY", "message": "...", "remediation": "Another process ...", "exit_code": 4}
```

The agent should act on `remediation`, not on parsing the human message.
