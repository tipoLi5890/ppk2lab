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

## Calibration provenance

Calibration constants come from the device and can arrive incomplete: a
truncated metadata reply, a field the parser does not recognize, a device
that reports no calibration at all. The danger is not the missing value but
what fills its place — `O[r] = 0` alone biases every sample in that range by
the full offset. Two properties prevent that:

1. Unknown constants can never become defaults — the affected range converts
   to `NaN` (below).
2. Every capture records *why* its numbers might be suspect. The manifest
   carries a `calibration` block (`calibrated_flag`, `metadata_terminated`,
   `metadata_warnings`, `missing_ranges`, `user_gains`), and the same facts
   surface as coded warnings (`W_NOT_CALIBRATED`,
   `W_CALIBRATION_INCOMPLETE`, `W_USER_GAIN`, `W_METADATA`) plus `doctor`
   checks.

The `Calibrated` metadata field is reported but not interpreted: real
hardware has been observed reporting `Calibrated: 0` while producing
plausible readings, and the flag's meaning is not hardware-verified. It is
surfaced as a warning, never as a reason to discard data.

A non-unity `UG` (user gain) scales every reading in its range. Because
ppk2lab can also write user gains, a value left behind by a previous session
would otherwise silently rescale a whole capture; it is warned about
explicitly.

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
