# Power analysis (offline, read-only)

## Commands

```bash
ppk2lab measure run.ppk2a --json                    # whole capture
ppk2lab measure run.ppk2a --window 0.10:0.25 --json # seconds from start
ppk2lab measure run.ppk2a --state-threshold 1mA --json
ppk2lab measure run.ppk2a --annotations ann.jsonl \
        --group-by annotation|kind|frame|transaction --json
```

`--window` reads only the chunks it falls in (see capture.md); anything
that has to load the whole artifact is capped by `--max-samples`.

Latency ("how fast back to sleep?"): use an assertion window
(`after uart("TX_DONE"), within 20ms, avg_current < 10uA` via
`ppk2lab assert`) or Python
`ppk2lab.capture.stats.latency_until_below(...)` — gaps reset the hold, so
missing data never counts as evidence of sleep.

## Which number answers the question

A mean and a median answer different questions, and on a duty-cycled load
they differ by orders of magnitude on the same capture:

- **mean** answers *how long will the battery last* — it is charge over
  time, the only figure that integrates correctly.
- **Check `distribution.quantiles_at_floor` before quoting a percentile.**
  Below 200 nA the log grid has no bin, so a quantile listed there is the
  floor, not a measurement, and `W_BELOW_MEASUREMENT_FLOOR` says so. On a
  near-idle input most samples land below the floor; report the mean, the
  minimum, or charge instead of a percentile that bottomed out.
- **p50** (`median_current`) answers *what is my sleep current* — the value
  the DUT actually sits at.
- **p90 / p99 / p999** answer *how bad do the bursts get* without resting on
  one sample. `max` is one sample and drifts upward with capture length as
  range-switch transients accumulate; a percentile does not.

On the shipped demo profile the mean is 1806.72 uA — a current the DUT
never draws for a single sample — while `p50` sits at 6.05 uA, the real
sleep current. Quoting the mean as "the sleep current" is the mistake this
section exists to prevent: quote both, and say which question each answers.

Percentiles come from a log-spaced histogram accumulated in the same pass
as the mean (fixed memory, whatever the capture length), so they are
quantized. `distribution.quantile_half_width_fraction` publishes the
half-width — 0.90% on the shipped grid of 128 bins per decade — and is
worth reporting when a percentile sits close to a threshold.
`distribution.below_grid_samples` counts samples at or below the 200 nA
grid floor, where a quantile can only be an upper bound.

## Duty cycle: `--state-threshold`

`--state-threshold 1mA` splits the window at that current and reports
samples, duration, mean, charge and run count for each side. The threshold
is echoed in the result and there is deliberately no default: "the DUT
sleeps 85% of the time" is a statement about a chosen boundary, not about
the DUT, and the same capture split at 10 uA and at 100 uA gives two
different, both correct, answers. Always report the threshold and why it
was chosen.

## Uncertainty: how many of the digits are real

Every result carries `uncertainty`, and `guaranteed` is always `false`:

- `mean_ua_typical`, `charge_uc_typical`, `energy_uj_typical` come from
  Nordic's *typical* per-range accuracy plus each range's own resolution
  step, summed **linearly**. These are systematic gain specifications,
  fully correlated within a range: they do not average away and do not
  shrink with capture length. The resolution term dominates at the bottom —
  a 1 uA reading in the lowest range is 10% of gain plus a 0.2 uA step,
  about ±30%, not ±10% — and that is exactly where low-power work lives.
- `mean_ua_batch_stderr` is a separate, separately labelled figure from
  sub-window batch means. It describes the spread of *this signal*, not the
  instrument's accuracy; never add the two together. Never compute σ/√N
  over individual samples either: 10 us samples through a shunt-switching
  front end are heavily autocorrelated, and that understates the spread by
  an order of magnitude.
- `model` names the constant set the figures came from, so a stored result
  can be told apart from a later revision of the model.

Human output rounds to the digits the interval supports; JSON keeps full
precision. Do not re-print more digits than the interval justifies.

## Interpreting results honestly

- `charge_uc` integrates uA over the 10 us grid. Energy is `charge × a
  voltage nobody measured`, so every result carries `voltage_basis` and
  `voltage_measured: false`:
  - `device_metadata`/`configured_source` in **source** mode — the DUT runs
    from VOUT, so the setpoint is defensible (the DUT terminal sees slightly
    less after shunt burden and lead drop);
  - **ampere** mode — the DUT runs from its own supply, which the meter never
    sees, so `energy_uj` is `null`. Ask the user for the real supply voltage
    and pass `--assume-voltage-mv 3300`; the result then reads
    `voltage_basis: "caller_override"`. Never guess it.
- `complete` means gap-free *and* populated. A window reaching past the
  capture's data is `complete: false` with `W_WINDOW_UNPOPULATED`, however
  clean the samples inside it are.
- `charge_is_lower_bound: true` means samples were excluded — gaps, an
  unpopulated span, missing calibration, implausible values, clipping, or a
  capture-level wall-clock deficit — so the integral understates reality;
  `covered_fraction` says how much of the window actually carries samples.
- `saturated_samples > 0` (`W_CLIPPED`): those readings sat on the ADC's
  full-scale code. The amplitude was pinned, not measured — a flat top
  there is the front end, not a regulated load — so `max` is a floor and
  the integral is a lower bound. `saturated_ranges` names where; in the top
  range it means the DUT drew more than the instrument's 1 A span.
- `samples.not_convertible > 0`: calibration missing for some ranges;
  identify them with `ppk2lab info --json`.
- Non-empty `sample_gaps`: quote the missing sample count; means and peaks
  exclude missing data while duration still counts gap time.
- `samples_per_range`, `charge_per_range_uc`, `range_switches` and
  `range_switch_rate_hz` show where the charge came from and how hard the
  front end was working.
- Quote peak current with `peak_index` (× 10 us = time) so it can be
  located in the waveform.
- Raw statistics are the default; `--filtered` adds range-switch spike
  smoothing without replacing the raw series.

## Output format

A compact table per window: duration, mean with its typical uncertainty,
min/max, p50/p90/p99, charge, energy, gaps — plus the `capture_sha256`
prefix so numbers stay traceable. On a windowed read that digest is `null`
with `W_PARTIAL_INTEGRITY`: report the `capture_id` and say why.

Done when: every reported figure carries the question it answers, its
uncertainty, and its coverage — and no lower-bound number is presented as
exact.
