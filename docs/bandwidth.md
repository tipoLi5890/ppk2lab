# Current channel: sampling model, bandwidth, and aliasing

`docs/logic-port.md` states what a fixed 100 kS/s means for D0-D7. This page
states the same thing for the current channel, which is the instrument's
whole point: **over what band is a reported current figure valid, and what
happens to the numbers when the DUT draws current faster than that.**

Nothing here is a defect in ppk2lab or in the PPK2. It is the sampling
theorem applied to a fixed 100 kS/s instrument, and the reason it needs
writing down is that aliasing does not look like an error — it looks like a
clean measurement of a load the DUT never drew.

## The short version

- One current sample every 10 us, on the same timeline as D0-D7. The rate is
  fixed; nothing in the tool or the hardware changes it (`docs/faq.md`).
- Periodic content **below 50 kHz** is represented at its true frequency.
- Content **above 50 kHz** folds back into 0-50 kHz and is then
  indistinguishable from real low-frequency current. No statistic computed
  from the samples can undo this — that is the theorem, not a limitation of
  the implementation.
- **Charge and mean current survive folding**, provided the window is long
  compared with the *folded* period — with one exception below.
- **Peak, min, and anything RMS-like do not survive folding**, and are
  understated even for ripple that is nominally resolved.
- An event shorter than about 10 us cannot be characterised by amplitude at
  all.

## Sampling model

The current channel and D0-D7 are one synchronised 100 kS/s timeline
(`docs/SPEC.md`, "Timeline"): one figure per 10 us sample period, no
averaging performed by ppk2lab, no interpolation across gaps. Everything on
this page follows from that plus the sampling theorem.

A sinusoid at frequency `f` sampled at `fs = 100 kHz` appears in the record
at the folded frequency

```text
f_alias = | f - round(f / fs) * fs |          # 0 .. fs/2
```

with its amplitude intact. Folding changes *when* the record says current
was high; it does not change how much of it there was — unless `f_alias`
lands on zero, which is the first of the two failures worth knowing by name.

## What folds where — measured

Measured end to end through ppk2lab's own path: a 1000.00 uA load carrying a
400 uA-amplitude sinusoidal ripple, built with `ppk2lab.testing.ArrayProfile`,
streamed by the built-in simulator, captured with `PPK2.capture()`, and
summarised by the same `compute_stats` that backs `ppk2lab measure`. 2 s /
200 000 samples per row. The DC reference row reads 1000.12 uA rather than
1000.00 — that 0.12 uA is the simulator's ADC quantisation in range 2, and it
is the noise floor against which the rest of the table should be read.

| Ripple | Appears in the record at | Reported mean | Reported min-max | Charge (true 2000.0 uC) |
|---|---|---:|---:|---:|
| none (DC) | — | 1000.12 uA | 1000.12 - 1000.12 | 2000.244 |
| 1 kHz | 1 kHz | 999.99 uA | 600.01 - 1399.93 | 1999.982 |
| 10 kHz | 10 kHz | 1000.00 uA | 600.32 - 1399.62 | 2000.000 |
| 25 kHz | 25 kHz | 999.97 uA | 686.69 - 1313.25 | 1999.939 |
| 49 kHz | 49 kHz | 999.99 uA | 600.01 - 1399.93 | 1999.982 |
| 50 kHz | 50 kHz | 999.97 uA | 686.69 - 1313.25 | 1999.939 |
| 51 kHz | **49 kHz** | 999.99 uA | 600.01 - 1399.93 | 1999.982 |
| 60 kHz | **40 kHz** | 1000.00 uA | 600.32 - 1333.39 | 2000.000 |
| 75 kHz | **25 kHz** | 999.97 uA | 686.69 - 1313.25 | 1999.939 |
| 99.9 kHz | **100 Hz** | 1000.00 uA | 600.01 - 1399.93 | 1999.996 |
| 100 kHz | **DC** | **1313.25 uA** | 1313.25 - 1313.25 | **2626.503** |
| 200 kHz | **DC** | **1313.25 uA** | 1313.25 - 1313.25 | **2626.503** |

Read the middle block first: 51, 60, 75 and 99.9 kHz all report the mean and
the charge correctly while placing the ripple at a frequency the DUT never
produced. A 60 kHz switching regulator shows up in a CSV export as a 40 kHz
ripple. Nothing in the record says so.

The 50 kHz row is a degenerate case rather than a working one: two samples
per period means the recorded amplitude is set entirely by phase, and at
some phases the ripple disappears from the record altogether. Treat 50 kHz
as the edge of the band, not as part of it.

This table is the sampling model measured through the real analysis path,
not a hardware measurement: the simulator takes one instantaneous value per
sample period by construction. Whether a physical PPK2 reproduces these rows
depends on what its analog front end does between sample instants, which the
project has not measured — see "What this project has not measured" below.

## Failure 1: ripple at the sample rate becomes a DC error

A 100 kHz ripple is sampled at exactly one point per cycle, so every sample
lands at the same phase and the ripple collapses to a constant offset. The
reported mean moves from a true 1000.00 uA to 1313.25 uA — 31% high — and:

- `complete` is `true`, `charge_is_lower_bound` is `false`, `warnings` is
  empty. There is no gap, because no samples were lost;
- min, max and mean are all the same number, so the record reads as a
  *perfectly regulated flat load* — the most trustworthy-looking shape a
  capture can have;
- charge is wrong by the same 31%, and **averaging for longer does not fix
  it**, because the alias is at DC. This is the one case where a longer
  window buys nothing.

The size of the error is set by the arbitrary phase between the DUT's
switching clock and the PPK2's sample clock, so it is not reproducible
either. Sweeping that phase over the same 1000.00 uA ± 400 uA load:

| Sampling phase | Reported mean |
|---|---:|
| 0.0 rad | 1000.12 uA |
| 0.5 rad | 1191.78 uA |
| 0.9 rad | 1313.25 uA |
| 1.5 rad | 1399.01 uA |
| 2.5 rad | 1239.39 uA |
| 3.5 rad | 859.73 uA |
| 4.5 rad | 608.86 uA |
| 5.5 rad | 717.82 uA |

The reported figure can be anywhere inside the ripple's own range, and two
captures of the same DUT will disagree. If a headline mean moves by tens of
percent between otherwise identical runs with no gaps and no warnings,
ripple near a multiple of 100 kHz is the first thing to suspect.

## Failure 2: ripple near the sample rate becomes a slow beat

A 99.9 kHz ripple folds to 100 Hz. The whole-capture summary is *exact* —
mean 1000.00 uA, charge 1999.996 uC against a true 2000.0 — while a 100 Hz,
±400 uA beat rides through the record that the DUT never drew.

That is harmless for a whole-capture number and ruinous for a per-event one.
Measuring the same capture in 1.04 ms windows (the length of one 9600-baud
byte, i.e. what `measure --annotations --group-by frame` does):

| Window | Reported mean | True mean |
|---|---:|---:|
| 0 | 1214.14 uA | 1000.00 uA |
| 1 | 969.76 uA | 1000.00 uA |
| 2 | 737.82 uA | 1000.00 uA |
| 3 | 613.93 uA | 1000.00 uA |
| 4 | 649.07 uA | 1000.00 uA |
| … | … | … |
| 8 | 1392.18 uA | 1000.00 uA |

Per-frame figures span 613.93 to 1392.18 uA — a factor of 2.3 between the
smallest and the largest — for a load whose true mean is 1000.00 uA in every
one of them. The summary the operator checks is clean; every per-event
energy figure derived from it is not.

This is why the robustness rule below is stated against the **folded**
period rather than the ripple period. A 99.9 kHz ripple needs a window long
compared with 10 ms, not compared with 10 us.

## Which statistics survive aliasing

**Robust** — use these when the DUT has content above 50 kHz:

- `charge_uc`, and `current_ua.mean` over a window that is long compared
  with the folded period `1 / f_alias`. Folding redistributes current in
  time; it does not create or destroy charge.
- The exception, and it is absolute: ripple at an exact multiple of 100 kHz
  folds to DC, and no window length recovers it.

**Not robust** — treat as a property of the capture, not of the DUT:

- `current_ua.max` / `peak_current` and `current_ua.min`. Folded ripple puts
  excursions in the record at the wrong times, and even *resolved* ripple
  understates the excursion whenever only a few samples land per period: the
  25 kHz row above records 686.69-1313.25 uA against a true 600-1400 uA, a
  22% understatement, because four samples per cycle rarely land on the
  peaks. The recorded peak is also phase-dependent, so it changes run to
  run.
- Anything RMS-like. RMS weights the fast content that aliasing corrupts
  most, which is why ppk2lab does not report an RMS current: it would be the
  most alias-sensitive number in the set, presented as the most confident.

`--filtered` does not help here. `SpikeFilter` suppresses range-switch
transients (`docs/calibration.md`); aliasing is not a transient, and the
folded ripple is indistinguishable from real signal to any filter operating
on the sampled series.

## Events shorter than about 10 us

An event shorter than one sample period cannot have its duration measured,
because the record has no resolution finer than 10 us to express it in. That
much holds regardless of front-end design, and it is the same statement
`docs/logic-port.md` makes for D0-D7 pulses. It matters most for exactly the
loads people buy a PPK2 to look at: a radio TX ramp, an inrush edge, a DMA
burst.

What happens to the event's *amplitude* depends on the front end, and the
two possibilities are far apart:

- **If the instrument point-samples**, a short event either coincides with a
  sample instant or does not. If it does not, it leaves no trace at all. If
  it does, its full amplitude is recorded against a 10 us-wide slot, so the
  charge it contributes is inflated by `10 us / true_duration`.
- **If the front end integrates over the sample period**, the event always
  leaves a trace and its charge is right, but the recorded height is the
  event's amplitude scaled down by `true_duration / 10 us` — so a 2 us,
  10 mA burst reads as 2 mA.

Either way the recorded height of a sub-10 us event is not that event's
amplitude. Ask for charge over a window that contains the whole burst plus
its surroundings, not for the height of the spike — and see "What this
project has not measured" for which of the two the PPK2 actually does.

## The practical remedy, and the question it forces

The physical fix is local decoupling at the DUT: enough capacitance close to
the load that the capacitor supplies the fast current and the PPK2 sees the
average draw refilling it. This works, and it is what you want for battery
sizing — but it is not free of consequences:

- **Charge and mean become right.** The high-frequency content never reaches
  the shunt, so there is nothing left to alias.
- **Peak current becomes meaningless.** You measured the capacitor's refill
  current, not the DUT's draw. The peak the DUT presents to its own supply
  rail is now invisible by design.

So decide which question you are asking *before* you choose the wiring:

| Question | Wiring | What to report |
|---|---|---|
| How long will the battery last? | decouple locally | `charge_uc`, `current_ua.mean` |
| How much current does the load pull at its worst instant? | minimal decoupling | not answerable at 100 kS/s — this needs an oscilloscope with a current probe or a shunt-and-scope setup |

A capture taken with heavy local decoupling and reported as a peak current
is the most common way to get a confidently wrong number out of this
instrument, and nothing in the artifact records which wiring was used. If it
matters, record it alongside the capture yourself — the artifact cannot.

## The one hardware figure this page can offer: the noise floor at DC

A hardware session has since run on one unit, and it measured this signal
path only at DC. With nothing connected to VOUT, 60 s of capture read mean
0.1633 uA, minimum −0.2477 uA, maximum 0.5867 uA; a second 60 s run read
0.1769 / −0.3356 / 0.6306. Every sample sat in range 0 and no range switch
occurred.

That is the floor any ripple measurement stands on — an excursion of a few
hundred nanoamps in range 0 is the sensor's own noise, whatever frequency
you attribute it to. It is **not** an answer to the question this page
asks. A static input says nothing about what the front end does between
sample instants, and no ripple of any frequency was applied to that unit.
The tables above remain the point-sampling model.

## What this project has not measured

**No -3 dB bandwidth figure is stated here, because none has been
measured.** The tables above are the *point-sampling* model. A front end
that instead integrates over the sample period would behave differently in
one specific and important way: a ripple whose period divides the 10 us
window averages to nothing inside it, so a 100 kHz ripple would contribute
no DC offset at all rather than the phase-dependent one in the table, and
the charge of a sub-10 us burst would survive even though its recorded
height would not.

Which of the two the PPK2 does is the single largest open question about
what a ppk2lab current figure means, and it is answerable in one load-side
experiment: drive a fixed-duty square-wave load whose DC-equivalent mean is
held constant at 5, 25, 49, 60, 100 and 200 kHz, and compare the reported
mean against that known truth. It needs no tool-driven power, mode, or
voltage change — the load is varied, the instrument only watches.

Until that has been run and recorded:

- treat charge and mean as defensible for loads whose content is below
  50 kHz, and as unverified above it;
- treat a load with ripple near 100 kHz as unmeasured, not as measured at
  1313 uA;
- do not quote a bandwidth number for this instrument, in either direction.

`hf_content_ua` and RMS current are both blocked on the same answer and are
deliberately not implemented.

## Reproducing the numbers on this page

No hardware required; the profiles and the simulator are part of the
package. The capture path here never touches DUT power.

```python
import math
from ppk2lab import PPK2
from ppk2lab.testing.profiles import ArrayProfile
from ppk2lab.transport.mock import SimulatedPPK2

fs, n = 100_000.0, 200_000          # 2 s at the fixed 100 kS/s
ripple_hz, amplitude_ua, dc_ua = 100_000.0, 400.0, 1000.0
wave = [dc_ua + amplitude_ua * math.sin(2 * math.pi * ripple_hz * i / fs + 0.9)
        for i in range(n)]

device = PPK2.open(simulator=SimulatedPPK2(profile=ArrayProfile(wave)))
result = device.capture(sample_limit=n)
device.close()
stats = result.stats
print(stats.mean_ua, stats.min_ua, stats.max_ua, stats.complete)
# 1313.25... 1313.25... 1313.25... True   (true mean: 1000.00 uA)
```

Change `ripple_hz` for the other rows. For the per-window tables use
`ppk2lab.capture.stats.compute_stats(result.capture, start_index=...,
end_index=...)`, which is what `ppk2lab measure --window` calls. Simulated
data tests the toolchain and, here, demonstrates the sampling model — it is
never a measurement of hardware.

## Related

- `docs/logic-port.md` — the same statement for D0-D7, plus wiring.
- `docs/faq.md` — why the rate is fixed, and why two tools disagree.
- `docs/calibration.md` — per-range accuracy and resolution, and what
  `--filtered` does.
- `docs/energy-analysis.md` — how charge and energy are integrated.
