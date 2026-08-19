# Calibration and current conversion

## Formula

For a sample in measurement range `r` with the device's calibration
constants (metadata families `R`, `GS`, `GI`, `O`, `S`, `I`, `UG`, each
indexed 0-4):

```text
adc = ADC_field * 4
x   = (adc - O[r]) * ((1.8 / 163840) / R[r])          # amperes: volts / ohms
current_A  = UG[r] * ( x * (GS[r] * x + GI[r]) + S[r] * (VDD_mV / 1000) + I[r] )
current_uA = current_A * 1e6
```

The expression is dimensionally amperes: the `R` constants are real shunt
resistances in ohms (verified on hardware — a real device reports
R0 ≈ 1000.6 Ω, and its S/I correction terms are ~1e-7, i.e. sub-microamp
offsets in amps). The public API reports microamperes. `VDD_mV` is the
configured source voltage; `Calibration.convert(...)` uses the metadata VDD
by default and accepts an override for captures taken at a different
setting.

A precision known-load cross-check against the official app remains a
release gate (ROADMAP.md, known-load cross-check); the unit derivation above is
confirmed by dimensional analysis and open-circuit noise-floor magnitude on
real hardware.

## Unknown values

- A range whose constants are missing, `NaN`, or whose `R` is zero converts
  to `NaN` — unknown calibration is preserved, never replaced by defaults.
  Affected ranges are listed by `ppk2lab info` and counted in statistics as
  `not_convertible`.
- Samples with an invalid range field (5-7) are kept with their raw values,
  flagged invalid, and excluded from statistics.
- Energy is `null` when the supply voltage is unknown (for example an
  Ampere-mode capture without usable metadata VDD).

## Official measurement ranges (reference)

| Range | Current span | Typical resolution | Typical accuracy (avg) |
|---|---|---:|---:|
| 0 | 200 nA - 50 uA | 0.2 uA | ±10% |
| 1 | 50 uA - 500 uA | 0.5 uA | ±10% |
| 2 | 500 uA - 5 mA | 5 uA | ±10% |
| 3 | 5 mA - 50 mA | 50 uA | ±10% |
| 4 | 50 mA - 1000 mA | 1000 uA | ±15% |

(Nordic official specification; see `docs/sources.md`.)

## Raw vs filtered series

Range switches can produce short transients. ppk2lab always reports the raw
calibrated series; an optional spike filter (`SpikeFilter`, `--filtered`)
additionally holds the last pre-switch value for a configurable number of
samples after each range change. The filter is an experimental heuristic:
its state resets across gaps, and it never replaces the raw series, so users
can verify measurements or apply their own filtering.

## Simulator constants

The simulated device publishes constants with `O=0, GS=0, GI=1, S=0, I=0,
UG=1` and per-range `R` chosen so that full ADC scale equals the official
range maxima. Simulated conversions are exact round-trips for testing the
pipeline — they are not measurements.
