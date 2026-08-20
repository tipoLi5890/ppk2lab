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
resistances in ohms. The public API reports microamperes. `VDD_mV` is the
configured source voltage; `Calibration.convert(...)` uses the metadata VDD
by default and accepts an override for captures taken at a different
setting.

The shunts read out of one physical unit (firmware fingerprint
`HW=49625 IA=59.0 keys=40 ports=2`, macOS host) are:

| Range | Shunt `R[r]` |
|---|---:|
| 0 | 1000.6250 Ω |
| 1 | 101.4608 Ω |
| 2 | 10.2309 Ω |
| 3 | 0.9629 Ω |
| 4 | 0.0559 Ω |

All five user gains on that unit were 1.0. Each shunt is close to a tenth of
the one below it, except R4 at about a seventeenth of R3 — the same ladder
the range table at the bottom of this page describes from the outside. It is
also why the instrument switches shunts at all: 200 nA through R0 develops
200 uV across it, while the same 200 nA through R4 would develop 11 nV.
These are one unit's constants, not a specification; each device carries its
own, and only this one has been read.

## Known-load cross-check

One has been run, on one unit, and it settles less than it appears to. A
680 kΩ ±5% resistor between VOUT and GND at that unit's existing 3700 mV
setpoint should draw, as a series circuit through the unit's own R0,
`3.700 / (680000 + 1000.625) = 5.4332 uA`. Eleven captures read between
5.5280 and 5.5614 uA — 5.55 uA, +2.1% from nominal. Every sample stayed in
range 0, with no range switches and no saturated samples.

That confirms there is no gross error: the reading is consistent with the
load. It **cannot** confirm the instrument's own gain error, because the
resistor's ±5% tolerance puts the true current anywhere in
[5.175, 5.719] uA — a window more than twice as wide as the 2.1% deviation
being checked. **No calibrated reference has been used, and a ±5% resistor
is not one.** Resolving gain error needs a resistor an order of magnitude
tighter or a calibrated current source; until that is run, the per-range
figures at the bottom of this page are Nordic's specification and not this
project's measurement. A cross-check that can resolve the gain error
remains a release gate (`ROADMAP.md`).

The same check puts a number on the `voltage_basis` caveat: at this current
the burden across R0 is 5.44 mV, 0.147% of the 3700 mV setpoint, so the
resistor saw 3.6946 V. Energy computed from the setpoint is high by that
much (docs/energy-analysis.md, "Voltage provenance"), and the fraction grows
in proportion to the current.

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

### `Calibrated: 0` on a device that carries constants

The `Calibrated` metadata field is reported but not interpreted, and the
one unit measured on hardware is why. It reports **`Calibrated: 0`** while
carrying a complete set of constants for all five ranges, and it converts:
the known-load cross-check above and the noise floor in
docs/energy-analysis.md were both taken on it. What that produces:

- `doctor` returns the `calibrated_flag` check as `warn`, detail `device
  reports Calibrated: 0`, remediation "absolute accuracy is unconfirmed; the
  flag's meaning is not hardware-verified". The neighbouring `calibration`
  check still passes with "all 5 ranges calibrated", because they are. On
  that unit `doctor` was 11 pass, 1 warn, 1 skip, exit 0;
- every capture taken from it carries `W_NOT_CALIBRATED`, and its manifest's
  `calibration` block records `calibrated_flag: false` beside the constants
  it actually used.

Why the flag reads 0 on a device that carries constants is **unexplained**.
It is written down rather than explained away: a user will meet the warning
and needs to know exactly how much it says. It says the device's own flag
disclaims calibration; it does not say the constants are absent, and it is
not by itself a reason to discard data. It has been seen on one unit, one
firmware fingerprint, one host — whether it is common, or particular to this
unit, is unknown.

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

(Nordic official specification; see `docs/sources.md`. This project has not
verified either column against a calibrated reference — see "Known-load
cross-check" above for what has been checked and how far it goes.)

This table is not illustrative. `RANGE_RESOLUTION_UA` and
`RANGE_TYPICAL_ACCURACY` in `src/ppk2lab/capture/stats.py` are a
transcription of its two right-hand columns and are what every
`uncertainty.*_typical` figure is computed from;
`tests/test_uncertainty.py` parses the rows above and fails if the two
disagree, because then one of them is lying about where the number came
from. Edit it only to correct a transcription error, and keep the row shape
parseable.

Two properties decide how the figures may be used. They are **typical**, not
guaranteed limits — Nordic publishes them that way — and they are **gain**
specifications, so the error is the same error on every sample taken in a
range and does not average away with capture length. The resolution column is
a separate additive term that dominates at the bottom of a range: a 1 uA
reading in range 0 is ±0.1 uA of gain plus ±0.2 uA of resolution, so ±30%,
not ±10%. The model is in docs/SPEC.md, "Measurement uncertainty", and what
it looks like in a result is in docs/energy-analysis.md.

## Raw vs filtered series

Range switches can produce short transients. ppk2lab always reports the raw
calibrated series; an optional spike filter (`SpikeFilter`, `--filtered`)
additionally holds the last pre-switch value for a configurable number of
samples after each range change. The filter is an experimental heuristic:
its state resets across gaps, and it never replaces the raw series, so users
can verify measurements or apply their own filtering.

**How long a switch actually takes to settle has not been measured.** The
default hold, `SpikeFilter(settle_samples=3)`, is a guess — 30 us on the
10 us grid — and `docs/api-baseline.md` lists it as explicitly not frozen
for that reason. The hardware session that produced the constants above put
no load near a range boundary: every capture in it sat 100% in range 0 and
recorded zero range switches, so it observed no switch to time. Measuring it
takes a load that steps across a boundary at a known instant; until that has
been run, do not build a threshold on the filtered series, and read a
`range_switches` count as the number of chances a capture had to catch a
transient rather than as a corrected quantity.

## Simulator constants

The simulated device publishes constants with `O=0, GS=0, GI=1, S=0, I=0,
UG=1` and per-range `R` chosen so that full ADC scale equals the official
range maxima. Simulated conversions are exact round-trips for testing the
pipeline — they are not measurements.
