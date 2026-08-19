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

- `charge_uc` integrates uA over the 10 us grid; `energy_uj = charge × VDD`.
  `energy_uj: null` means the supply voltage is unknown — say so; never
  substitute a guessed voltage.
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
