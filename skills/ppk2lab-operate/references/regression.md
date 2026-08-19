# CI power regression (offline, read-only)

## Rule design

DSL: `[after <event>,] [within <duration>,] <metric> <op> <value>` with
explicit units. Events: `uart("TEXT"[, D2])`, `spi(0x9f, 0x00)`,
`digital(D3 rising)`. Metrics: `avg_current`, `max_current`/`peak_current`,
`min_current`, `charge`, `energy`.

```text
after uart("TX_DONE"), within 20ms, avg_current < 10uA
after spi(0x9f), within 5ms, charge < 2uC
max_current < 80mA
```

Derive thresholds from a measured baseline plus margin (e.g. baseline mean
+ 20%), never from datasheet hopes. Version the rules in a `rules.json`
(array of DSL strings or rule objects) next to the firmware.

## Run

```bash
ppk2lab capture --device S --duration 10s --output run.ppk2a --json
ppk2lab assert run.ppk2a --rules-file rules.json \
  --uart-rx D0 --baud 9600 --format junit --output report.xml
```

Exit codes: 0 all passed; 1 failed or anchor event missing; **6 = an
evaluation window overlaps missing data — neither pass nor fail; rerun the
capture rather than ignoring it.**

## Evidence discipline

- Archive the capture (or at least `capture_sha256`) with the verdict;
  every outcome embeds the hash.
- Keep the JSON report too (`--format json --output report.json`): observed
  values, thresholds, and exact evidence windows per event occurrence.
- For firmware A/B, run identical rules on both captures and diff
  `observed` values, not just pass/fail.
