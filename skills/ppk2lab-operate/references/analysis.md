# Power analysis (offline, read-only)

## Commands

```bash
ppk2lab measure run.ppk2a --json                    # whole capture
ppk2lab measure run.ppk2a --window 0.10:0.25 --json # seconds from start
ppk2lab measure run.ppk2a --annotations ann.jsonl \
        --group-by annotation|kind|frame|transaction --json
```

Latency ("how fast back to sleep?"): use an assertion window
(`after uart("TX_DONE"), within 20ms, avg_current < 10uA` via
`ppk2lab assert`) or Python
`ppk2lab.capture.stats.latency_until_below(...)` — gaps reset the hold, so
missing data never counts as evidence of sleep.

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
- `charge_is_lower_bound: true` means samples were excluded (gaps, missing
  calibration, implausible values), so the integral understates reality;
  `covered_fraction` says how much of the window actually carries samples.
- `samples.not_convertible > 0`: calibration missing for some ranges;
  identify them with `ppk2lab info`.
- `complete: false` / non-empty `sample_gaps`: quote the missing sample
  count; means and peaks exclude missing data while duration still counts
  gap time.
- Quote peak current with `peak_index` (× 10 us = time) so it can be
  located in the waveform.
- Raw statistics are the default; `--filtered` adds range-switch spike
  smoothing without replacing the raw series.

## Output format

A compact table per window: duration, mean/min/max current in sensible SI
units, charge, energy, gaps — plus the `capture_sha256` prefix so numbers
stay traceable.
