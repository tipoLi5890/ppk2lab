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
- **p50** (`median_current`) answers *what is my sleep current* — the value
  the DUT actually sits at, provided it is a measurement and not the grid
  floor (next section).
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

## When a percentile is a bound, not a measurement

**Check `distribution.quantiles_at_floor` before quoting any percentile.**
The grid is logarithmic and starts at 200 nA, so it has no bin below that
and none at or below zero — which an unloaded input legitimately produces.
A quantile named there was served from the floor itself; it bounds the true
value from above and does not measure it. `W_BELOW_MEASUREMENT_FLOOR` fires
and `distribution.below_grid_samples` counts how many samples were involved.

Measured on hardware with nothing drawing current, over 60 s: mean
0.1633 uA, min −0.2477 uA, max 0.5867 uA, and **69-71% of samples below the
200 nA floor**, so `p50` came back as exactly 200 nA. On that capture the
mean, the minimum and the charge are the honest answers to "what is the
sleep current"; the median is not one. `quantiles_at_floor` was empty and
`below_grid_samples` zero on the same session's 5.5 uA load, so the flag
tracks the signal rather than the setup.

Two things that same record confirms, and that a report should not treat as
faults:

- **Negative readings are real** at very low load. A correctly calibrated
  zero has noise on both sides of it; the minimum above is not a bug.
- **The error bar can exceed the reading.** `mean_ua_typical` came back as
  0.2177 uA on a mean of about 0.17 uA — ±123%. That is the resolution term
  doing its job at the bottom of range 0, not a defect. Report the interval;
  do not quote the mean as if it were tight.

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

## How well is absolute accuracy actually known?

Not well, and say so. One known-load cross-check exists: a 680 kΩ ±5%
resistor from VOUT to GND at that unit's 3700 mV setpoint. Expected as a
series circuit through the unit's own 1000.6250 Ω range-0 shunt,
`3.700 / (680000 + 1000.625)` = 5.4332 uA. Measured 5.55 uA across eleven
captures (5.5280-5.5614), i.e. +2.1% from nominal.

That confirms the absence of a gross error and nothing finer. The
resistor's ±5% tolerance puts the truth anywhere in [5.175, 5.719] uA,
which swallows the deviation whole: **the check cannot resolve the
instrument's own gain error and must not be reported as if it did.**
Resolving it needs a resistor an order of magnitude tighter, or a
calibrated reference; the project has used neither. Quote the `uncertainty`
block as the accuracy claim and call the absolute scale unverified.

The same measurement is a concrete instance of the `voltage_basis` caveat.
At 5.55 uA the shunt burden was 5.44 mV — 0.147% of the setpoint — so the
resistor saw 3.6946 V, not the 3.700 V the energy figure is computed from.
The DUT terminal always sees less than the setpoint; here that is how much.

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
  smoothing without replacing the raw series. Its settling window is an
  unmeasured assumption: every hardware capture so far sat 100% in range 0
  with zero range switches, so no load at a range boundary has ever been
  recorded. Prefer the raw series, and if a filtered figure changes a
  conclusion, report both.

## Output format

A compact table per window: duration, mean with its typical uncertainty,
min/max, p50/p90/p99, charge, energy, gaps — plus the `capture_sha256`
prefix so numbers stay traceable. On a windowed read that digest is `null`
with `W_PARTIAL_INTEGRITY`: report the `capture_id` and say why.

Done when: every reported figure carries the question it answers, its
uncertainty, and its coverage — and no lower-bound number is presented as
exact.
