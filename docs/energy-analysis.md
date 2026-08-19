# Charge, energy, and latency definitions

## Units and integration

- Current: microampere (uA), from the calibration formula
  (`docs/calibration.md`).
- Charge: microcoulomb (uC). Rectangle-rule integration on the fixed grid:
  `charge_uc = Σ current_ua × 10e-6` over stored, convertible samples.
- Energy: microjoule (uJ): `energy_uj = charge_uc × (voltage_mv / 1000)`.
  The PPK2 measures current only, so *which* voltage this is matters more
  than the arithmetic; see "Voltage provenance" below. When no defensible
  voltage exists, energy is `null` — never guessed.
- Duration: timeline span in seconds (`(end_index - start_index) × 10 us`),
  which includes missing samples so a gappy window is not reported shorter
  than it was.

## Voltage provenance

Every energy figure is `charge x an assumed voltage`, because no PPK2 ever
measures the DUT's terminal voltage. Results therefore carry
`voltage_measured: false` and a `voltage_basis`:

| `voltage_basis` | Meaning | Energy |
|---|---|---|
| `caller_override` | the caller passed the DUT's supply voltage explicitly | computed |
| `configured_source` | this session set VOUT and the DUT runs from it | computed |
| `device_metadata` | read from the device's VDD field (a regulator setpoint) | computed in Source mode only |
| `unknown` | no voltage at all | `null` |

In **Ampere Meter** mode the DUT is powered by its own supply, so the
device's VDD field describes nothing about it; energy is `null` unless the
caller supplies the real voltage (`--assume-voltage-mv`, or
`assume_voltage_mv=` in Python). In **Source Meter** mode the setpoint is a
defensible supply voltage, but still a setpoint: the DUT terminal sees
slightly less once shunt burden and lead resistance take their share, so
energy is a small overestimate at high current. `energy_note` states which
caveat applies to the number in hand.

The distinction is not pedantry: the same charge multiplied by a stale 5,000
mV setpoint instead of a real 1,800 mV supply overstates energy by 2.8x.

## Window statistics

`measure_window` / `ppk2lab measure` return (schema `window-stats`): the
exact window; stored, missing, unpopulated, invalid, not-convertible,
implausible and saturated sample counts; `covered_fraction`; mean/min/max
current with the peak's timeline index; charge with `charge_is_lower_bound`;
energy with its voltage provenance; a `complete` flag; per-range occupancy;
and the clipped gap list. NaN samples (missing calibration) are excluded from
statistics but counted.

Three fields exist specifically so a window that does not support its own
numbers cannot masquerade as one that does:

- `covered_fraction` — the share of the window that actually carries
  samples (`null` when an unknown-size gap makes the span unknowable);
- `complete` — gap-free **and** populated end to end. "No gap event fell
  inside this window" is a weaker statement and is not what this flag means:
  a 10 s window over a 50 ms capture contains no gaps because it contains
  almost nothing. Window positions holding neither a stored sample nor a
  recorded gap are counted in `samples.unpopulated`;
- `charge_is_lower_bound` — true whenever charge understates what the DUT
  drew. Gaps are the obvious cause, but a sample excluded for any other
  reason is charge that happened and was not counted, and so is a sample
  pinned at the ADC's ceiling, an unpopulated stretch, and loss the wall
  clock witnessed but the 6-bit counter could not describe.

`implausible` counts samples whose converted current exceeds what the
hardware can carry (>1.1 A). They are excluded from statistics rather than
averaged in: such values mean the 4-byte framing lost sync, not that the DUT
drew 9 A.

### Saturation

`saturated_samples` / `saturated_ranges` count valid samples whose raw ADC
field sat on full scale. This is the failure mode a plausibility bound cannot
catch: a pinned code converts to the top of its range — about 1 A in range 4,
below the >1.1 A implausibility bound — so a 2.5 A load reads back as
`mean = max = 1000000 uA`, a 2.5x error rendered as a perfectly regulated
flat top. Saturation therefore forces `charge_is_lower_bound` and emits
`W_CLIPPED` naming the range. In the top range it means the DUT drew more
than the instrument can carry; in a lower range it means the auto-range
switch had not completed yet.

### Range occupancy

`samples_per_range`, `charge_per_range_uc`, `range_switches` and
`range_switch_rate_hz` decompose the window across the five shunts. They are
what the uncertainty model below is computed from, and the switch count is
what makes an unfiltered `max_current` a property of the capture rather than
of the DUT (see "Why p99 rather than max").

## Distribution: what the DUT actually draws

A mean answers "how fast does this drain a battery". It is the wrong answer
to "what is my sleep current", and on a duty-cycled load the two questions
have wildly different answers. Run the shipped simulated profile for three
seconds and the tool reports:

```
mean 1806.72 uA      p50 6.05 uA      p90 11977 uA      max 12000 uA
```

That profile has three states and no others: 12 mA for 15% of the time,
25 uA for the 8.33% while the UART frame is on the wire, and 6 uA asleep for
the remaining 76.67%. It never draws 1806.72 uA for a single sample — that
value is arithmetically correct and describes no state the device is ever in.
`p50` is the sleep current, and it was sitting in the data all along.

Results therefore carry `current_ua.p50/p90/p99/p999` and a `distribution`
block describing the grid they came from.

### Why p99 rather than max

`max_current` is a defensible CI threshold only if it is a property of the
DUT. It is not: it is a property of the **capture**. The PPK2 auto-switches
between five shunts, and a range switch produces a short transient that the
raw series faithfully records (docs/calibration.md, "Raw vs filtered
series"). The number of range switches grows with capture length — the demo
profile makes 59 of them in three seconds, and would make roughly 71,000 in
an hour — so a longer capture gets more chances to catch one, and
`max_current` drifts upward with nothing about the DUT having changed. The
same is true of any single-sample outlier: a `max` is one sample out of
360 million in an hour-long run.

`p99` and `p999` are stable under capture length in a way `max` is not,
because they describe a fraction of the distribution rather than its extreme.
Prefer them for assertions — the metrics are `p50_current` (alias
`median_current`), `p90_current`, `p99_current`, `p999_current` — and keep
`max_current` for "did anything at all ever exceed this", which is a
different and much weaker question.

### How the quantiles are computed

From a fixed log-spaced histogram accumulated in the same pass as everything
else — 200 nA to 1 A at 128 bins per decade — never from stored samples. That
matters for three reasons a consumer can feel:

- **Bounded memory.** The state is the same size for a 50 ms capture and an
  8-hour one, which is what lets the statistics run live during acquisition.
- **A quantile is quantized.** The grid contributes up to ±0.90% of relative
  error, about a tenth of the instrument's own typical ±10%, and it is
  published as `distribution.quantile_half_width_fraction` rather than left
  for you to guess.
- **The grid has a floor.** Below 200 nA — the instrument's own range 0
  resolution — there is no bin. Those samples are counted in
  `distribution.below_grid_samples`, and a quantile served from them is
  reported at the floor or at the observed minimum, whichever is lower. It is
  an upper bound, not an estimate. Readings above 1 A are still binned but
  are counted separately in `above_grid_samples`, because a reading there is
  on the far side of the shunt the hardware was using.

Reported quantiles are always clamped into the observed `[min, max]`: the
grid never produces a value that was not measured.

## Duty-cycle decomposition

`--state-threshold <current>` (`state_threshold_ua=` in Python) splits a
window into "below" and "above" that current and reports, for each side, the
sample count, duration, mean current, charge, and the number of **runs** —
stretches of consecutive stored samples in that state.

```json
"state_split": {
  "threshold_ua": 1000.0,
  "below": {"samples": 255000, "duration_s": 2.55, "mean_ua": 7.86,
            "charge_uc": 20.05, "runs": 30},
  "above": {"samples": 45000, "duration_s": 0.45, "mean_ua": 12000.24,
            "charge_uc": 5400.11, "runs": 30}
}
```

Four things to know:

- `threshold_ua` is always reported back, because the split is a function of
  it. The same capture split at 10 uA and at 1000 uA gives two different,
  both correct, answers, and a split without its threshold cannot be read.
- **A split is two buckets, not two states.** The `below` mean above is
  7.86 uA while `p50` is 6.05 uA, because "below 1 mA" pools the 6 uA sleep
  state with the 25 uA UART state. Two buckets is what you asked for; how
  many states the DUT has is a different question, and the distribution
  answers it.
- A **gap ends a run**. Two samples separated by missing data are not
  consecutive, so an excursion that resumes after a gap is counted as a
  second observed run rather than asserted to be a continuation of the first.
- `state_split` is `null` when no threshold was given. Every other statistic
  here is collected unconditionally, so a capture's own recorded numbers and
  a later `measure` of the same window always agree.

## Uncertainty

Every window result carries an `uncertainty` block holding two quantities
that answer different questions and must not be added together. The model,
its derivation, and the constants are in docs/SPEC.md, "Measurement
uncertainty"; this is what it looks like in use.

```json
"uncertainty": {
  "model": "per_range_typical_v1", "guaranteed": false,
  "mean_ua_typical": 188.34, "charge_uc_typical": 565.03,
  "energy_uj_typical": 1695.08,
  "mean_ua_batch_stderr": 116.35, "batch_count": 37, "batch_samples": 8108
}
```

**`*_typical` — is the instrument reading true?** Built from Nordic's typical
per-range accuracy and resolution (docs/calibration.md). These are *typical*
figures, not guaranteed limits, and every field says so. They are systematic
gain specifications, so the per-range terms are summed **linearly**: the bar
does not shrink as the capture gets longer, because a gain error is the same
error on every sample. The resolution term is separate and necessary — a 1 uA
sleep current in range 0 is ±10% of gain *plus* the range's own 0.2 uA step,
so ±30%, and a purely multiplicative model would have understated it
threefold.

**`mean_ua_batch_stderr` — would this mean move if the window moved?** A
purely statistical figure from the spread of ~20-40 sub-window batch means.
It is deliberately **not** σ/√N: samples 10 us apart through a
shunt-switching front end are heavily autocorrelated, and on the demo profile
σ/√N gives 7.8 uA where the batch estimate gives 116 uA. The naive figure
would present a duty-cycled measurement as fifteen times more settled than it
is. It is `null` when the window is too short to hold two batches — one batch
has no spread, and reporting zero would claim perfect repeatability from a
single observation.

**Precision.** JSON keeps full precision. Human-readable output is rounded to
the digits the interval supports: `1810 ± 190 uA`, not
`1806.719544039553`. An interval of ±190 does not justify thirteen digits.

## Event-level energy

`measure_annotations` computes the same statistics over each annotation's
`[start_sample, end_sample)` window — per UART frame, per SPI word or
transaction — or aggregates per kind (count, total charge/energy, total
duration, worst peak, number of incomplete windows). Because annotations
carry exact sample ranges, every energy figure is reproducible from the raw
capture.

## Latency

`ppk2lab.capture.stats.latency_until_below(capture, from_index, threshold_ua,
hold_samples)` returns the timeline index where current first stays below a
threshold for `hold_samples` consecutive stored samples — e.g. wake-to-sleep
latency after a `TX_DONE` message. Gaps reset the hold: missing data never
counts as evidence of sleep.

## Assertions

The assertion engine (`docs/cli-reference.md#assert`) evaluates metrics over
either the whole capture or windows anchored at decoded events
(`after uart("TX_DONE"), within 20ms, avg_current < 10uA`). Outcomes:

- `passed` / `failed` — with the observed value, threshold, and evidence
  window per event occurrence;
- `no_event` — the anchor event was not found (fails CI, exit 1);
- `incomplete` — the window could not be evaluated as written; deliberately
  neither pass nor fail (exit 6).

### The window is evaluated as written

`within 20ms` asks about 20 ms. If the capture ends 5 ms after the anchor,
there is no 20 ms window to evaluate, and trimming the window to the
available data answers a question nobody asked — quietly, and usually in the
direction that passes. Every observation therefore carries
`covered_fraction` and `capture_end_sample` alongside the window it used, and
an unevaluable one carries a `reason_code`:

| `reason_code` | The window … |
|---|---|
| `sample_gaps` | overlaps missing samples |
| `window_past_capture_end` | extends past the last sample the capture holds |
| `window_unpopulated` | holds positions carrying neither a sample nor a recorded gap (a triggered capture starts mid-stream) |
| `metric_not_computable` | is fine, but the metric is `null` (missing calibration, or energy with no defensible voltage) |

### Anchors are not stitched together

An `after uart("TX_DONE")` anchor matches only bytes that were observed back
to back on the wire, and an `after spi(0x9f, 0x00)` anchor only words from
one CS transaction. A pattern spanning a gap, an unsynchronized frame, a
frame error, or two transactions is not evidence that the event happened;
those occurrences are rejected, and if none survive the outcome is
`no_event` with a warning naming how many were rejected and why —
distinguishable from a plain "never present". The rule and its rationale are
in `docs/decoders.md`, "Anchoring on decoded content".

Results embed `capture_id` and `capture_sha256`; store them with the verdict
so power regressions in CI remain auditable against raw captures. `--format
junit` renders the same outcomes for CI dashboards (`incomplete`/`no_event`
map to JUnit errors, `failed` to failures), and `<system-out>` carries the
evidence blob as JSON.
