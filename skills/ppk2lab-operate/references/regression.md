# CI power regression (offline, read-only)

## Rule design

DSL: `[after <event>,] [within <duration>,] <metric> <op> <value>` with
explicit units. Events: `uart("TEXT"[, D2])`, `spi(0x9f, 0x00)`,
`digital(D3 rising)`. Metrics: `avg_current`/`mean_current`,
`max_current`/`peak_current`, `min_current`, `p5_current`, `p50_current`
(alias `median_current`), `p90_current`, `p95_current`, `p99_current`,
`p999_current`, `charge`, `energy`.

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

**A low quantile rule says for itself whether it measured anything.** Below
the 200 nA distribution floor a quantile is a bound rather than a
measurement (analysis.md), and on hardware with nothing drawing current
`p50` came back as exactly the floor. Every assertion observation now
carries `quantiles_at_floor` and `below_grid_fraction` alongside the
observed value, `covered_fraction` and `capture_end_sample`, so a
floor-served quantile is visible in the report itself rather than only in a
separate `measure` run. When the rule's own metric is listed there and the
verdict would change for any smaller true value, the outcome is
`incomplete` with `reason_code: metric_at_measurement_floor` rather than a
pass. Read `quantiles_at_floor`; when the current the rule targets is in
that regime, assert on `avg_current` or `charge` instead. `p5_current` is
the first to go: `quantiles_at_floor` is monotone in rank, so a capture
whose `p50` is honest can still have its `p5` served from the floor.

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

**Have an exit-6 policy anyway.** Through `0.2.0`, `incomplete` was the
normal outcome for any capture longer than ten seconds — this project's own
artifact writer stalled the consumer at every chunk boundary, so a rule over
a whole capture almost never returned a verdict. That is fixed (see the
`0.3.0`) and gap-free captures are now the expectation,
which means a rule that comes back `incomplete` is worth investigating
rather than routine. A CI gate still needs a policy decided in advance —
retry the capture a bounded number of times, or record the run as not
evaluated. Both are honest. Widening the rule until the gap stops mattering
is not: the verdict genuinely is unknown from that artifact.

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
  `observed` values, not just pass/fail. For the size of the change itself
  use `ppk2lab compare baseline.ppk2a candidate.ppk2a`, which prices the
  delta rather than the two readings (analysis.md).

Done when: every rule reports `passed`, `failed`, `no_event` or
`incomplete` with its reason, and no `incomplete` or `no_event` has been
reported as a pass.
