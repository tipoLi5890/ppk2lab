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
exact window, stored/missing/invalid/not-convertible/implausible sample
counts, `covered_fraction`, mean/min/max current with the peak's timeline
index, charge with `charge_is_lower_bound`, energy with its voltage
provenance, a `complete` flag, and the clipped gap list. NaN samples
(missing calibration) are excluded from statistics but counted.

Two fields exist specifically so a gappy window cannot masquerade as a
complete one:

- `covered_fraction` — the share of the window that actually carries
  samples (`null` when an unknown-size gap makes the span unknowable);
- `charge_is_lower_bound` — true whenever gaps overlap the window, because
  the missing samples carried charge that no integral can recover.

`implausible` counts samples whose converted current exceeds what the
hardware can carry (>1.1 A). They are excluded from statistics rather than
averaged in: such values mean the 4-byte framing lost sync, not that the DUT
drew 9 A.

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
