# Charge, energy, and latency definitions

## Units and integration

- Current: microampere (uA), from the calibration formula
  (`docs/calibration.md`).
- Charge: microcoulomb (uC). Rectangle-rule integration on the fixed grid:
  `charge_uc = Σ current_ua × 10e-6` over stored, convertible samples.
- Energy: microjoule (uJ): `energy_uj = charge_uc × (source_voltage_mv /
  1000)`, using the configured source voltage. When the supply voltage is
  unknown, energy is `null` — never guessed.
- Duration: timeline span in seconds (`(end_index - start_index) × 10 us`),
  which includes missing samples so a gappy window is not reported shorter
  than it was.

## Window statistics

`measure_window` / `ppk2lab measure` return (schema `window-stats`): the
exact window, stored/missing/invalid/not-convertible sample counts,
mean/min/max current with the peak's timeline index, charge, energy, the
source voltage used, a `complete` flag, and the clipped gap list. NaN
samples (missing calibration) are excluded from statistics but counted.

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
- `incomplete` — an evaluation window overlaps missing data; deliberately
  neither pass nor fail (exit 6).

Results embed `capture_id` and `capture_sha256`; store them with the verdict
so power regressions in CI remain auditable against raw captures. `--format
junit` renders the same outcomes for CI dashboards (`incomplete`/`no_event`
map to JUnit errors, `failed` to failures).
