# CI power regression (offline, read-only)

## Rule design

DSL: `[after <event>,] [within <duration>,] <metric> <op> <value>` with
explicit units. Events: `uart("TEXT"[, D2])`, `spi(0x9f, 0x00)`,
`digital(D3 rising)`. Metrics: `avg_current`/`mean_current`,
`max_current`/`peak_current`, `min_current`, `p50_current`
(alias `median_current`), `p90_current`, `p99_current`, `p999_current`,
`charge`, `energy`.

```text
after uart("TX_DONE"), within 20ms, avg_current < 10uA
after spi(0x9f), within 5ms, charge < 2uC
p99_current < 80mA
```

**Prefer `p99_current` to `max_current` for a burst ceiling.** `max` is a
single sample, and range-switch transients accumulate, so a `max_current`
threshold drifts upward with capture length: the same firmware fails a
longer run. A percentile describes a fraction of the distribution and does
not drift. Use `max_current` only when the rule genuinely is "no sample may
ever exceed this".

**A low `p50_current` rule needs checking against `measure` first.** Below
the 200 nA distribution floor a quantile is a bound rather than a
measurement (analysis.md), and on hardware with nothing drawing current
`p50` came back as exactly the floor. An assertion observation carries the
observed value, `covered_fraction` and `capture_end_sample` — it does
**not** carry `distribution.quantiles_at_floor` or
`W_BELOW_MEASUREMENT_FLOOR`, so a floor-served quantile passes a
sleep-current rule with nothing in the report to say so. Run
`ppk2lab measure CAPTURE --json` on a representative baseline, confirm
`quantiles_at_floor` is empty at the current the rule targets, and assert
on `avg_current` or `charge` instead when it is not.

Derive thresholds from a measured baseline plus margin (e.g. baseline mean
+ 20%), never from datasheet hopes. Version the rules in a `rules.json`
(array of DSL strings or rule objects) next to the firmware. A rule object
needs `metric`, `op`, and a **finite** numeric `value`; a NaN or infinite
threshold, a missing field, a non-numeric `within_s`, or a non-object
`after` is now refused with a usage error (exit 2) instead of quietly
producing a failed build from a comparison that never happened.

## Run

```bash
ppk2lab capture --device S --duration 10s --output run.ppk2a --json
ppk2lab assert run.ppk2a --rules-file rules.json \
  --uart-rx D0 --baud 9600 --format junit --output report.xml
```

Exit codes: 0 all passed; 1 a rule failed or its anchor event was not found
(`no_event`); **6 = at least one window could not be evaluated as written —
neither pass nor fail. Rerun the capture; never treat it as a pass.**

## When a rule comes back `incomplete`

An `incomplete` outcome names why, per observation, in `reason_code`:

| `reason_code` | What happened | What to do |
|---|---|---|
| `sample_gaps` | a gap overlaps the evaluation window | rerun with less host load; the verdict is unknowable from this artifact |
| `window_past_capture_end` | the window as written runs past the end of the capture | capture longer, or shorten `within` — the window is *not* trimmed to fit |
| `window_unpopulated` | the window's positions hold neither a sample nor a recorded gap | same as above; check `covered_fraction` |
| `metric_not_computable` | the metric is `null` — usually `energy` with no defensible voltage | pass `--assume-voltage-mv`, or assert on `charge` |
| `metric_at_measurement_floor` | a quantile threshold the grid could only bound from above; the verdict would flip for a smaller true value | assert on the mean, the minimum, or charge at this current |

Every observation also carries `covered_fraction` and `capture_end_sample`,
so a report can say exactly how much of the asked-about window existed.

**Expect `incomplete` to happen on healthy hardware.** Ordinary host-queue
loss is enough: a capture with about 1% missing returned `incomplete` with
`reason_code: sample_gaps` and `covered_fraction: 0.9892`, and 0.43% loss
over 60 s was the *idle* rate on the host measured (capture.md). A CI gate
therefore needs an exit-6 policy decided in advance — retry the capture a
bounded number of times, or record the run as not evaluated. Both are
honest. Widening the rule until the gap stops mattering is not: the verdict
genuinely is unknown from that artifact.

## Anchors are not matched across a discontinuity

An `after` event is only matched on bytes or words that were observed back
to back: never across a sample gap, an unsynchronized or errored UART
frame, or two SPI CS transactions. Whenever candidate occurrences are
rejected for that reason the outcome carries a warning counting them and
naming why; if none survive, the rule comes back `no_event` (exit 1).

A pattern stitched across a discontinuity is not evidence the event
happened — a suite that used to pass on one was passing on a sequence that
was never on the wire. Fix the capture or the wiring; do not relax the rule
to recover the old verdict.

An `energy` rule needs a defensible supply voltage. Against an ampere-mode
capture the observed value is `null` and the rule cannot be evaluated; pass
`--assume-voltage-mv` with the DUT's real supply, and record that assumption
alongside the threshold so the verdict stays reproducible. `charge` rules
need no voltage at all and are the safer choice when the supply is unknown.

## Evidence discipline

- Archive the capture (or at least `capture_sha256`) with the verdict;
  every outcome embeds the hash.
- Keep the JSON report too (`--format json --output report.json`): observed
  values, thresholds, and exact evidence windows per event occurrence.
- In the JUnit report each `<system-out>` is the outcome serialized as
  JSON, so a CI dashboard's evidence blob can be parsed rather than read.
- For firmware A/B, run identical rules on both captures and diff
  `observed` values, not just pass/fail.

Done when: every rule reports `passed`, `failed`, `no_event` or
`incomplete` with its reason, and no `incomplete` or `no_event` has been
reported as a pass.
