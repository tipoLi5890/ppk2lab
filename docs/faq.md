# FAQ

Answers to the questions the PPK2 ecosystem asks most often, including the
ones whose honest answer is "the hardware cannot do that".

## Can I change the sample rate?

No. The PPK2 samples at a fixed 100 kS/s and that is what it streams; there
is no command to change it. Tools that offer a "sampling rate" setting are
choosing how many of those samples to keep, which is a different thing.

If you want smaller files or coarser data, reduce after the fact rather than
pretending the acquisition changed: export a window (`export --window`), or
decimate (`export --decimate N` / `--bucket-ms M`), which reports mean, min,
and max per bucket. A bare mean would hide exactly the current spikes you
bought this instrument to see. The bucket record and its rules are in
`docs/decimation.md`; the artifact always keeps every sample either way.

The fixed rate also fixes the band over which a reading means anything —
see "Over what frequency range is a current reading valid?" below.

## Over what frequency range is a current reading valid?

One sample every 10 us, so periodic content below 50 kHz is represented at
its true frequency and content above 50 kHz folds back into that band, where
it is indistinguishable from real low-frequency current. `docs/logic-port.md`
says the same thing about D0-D7; `docs/bandwidth.md` says it for current,
with worked numbers.

The two consequences worth carrying around:

- **Charge and average current survive folding**, as long as the window is
  long compared with the *folded* period — the one exception being ripple at
  an exact multiple of 100 kHz, which folds to DC and shifts the mean by an
  amount no averaging removes.
- **Peak, min, and anything RMS-like do not survive**, which is why ppk2lab
  reports no RMS current.

A DUT with a switching regulator near 100 kHz, or a current burst shorter
than about 10 us, is not measured by this instrument the way you probably
think it is. `docs/bandwidth.md` covers what to do about it — and why the
usual fix (local decoupling) makes charge right and makes peak meaningless,
so you have to decide which question you are asking first.

## Why do my numbers differ from another tool's?

Rather than guess at another tool's internals, here is what ppk2lab does, so
you can account for the difference:

1. **Filtering.** ppk2lab reports the raw calibrated series by default.
   Range switches produce short transients, and any tool that smooths them
   will report different peaks and a different integral. `--filtered` adds a
   smoothed series alongside the raw one; see `docs/calibration.md`.
2. **Integration window.** Averages depend on exactly which samples are
   included, so ppk2lab always reports the sample window it used.
3. **Missing samples.** ppk2lab averages only over samples that exist,
   reports `covered_fraction`, and marks charge as a lower bound when gaps
   overlap the window.
4. **Accuracy.** Nordic specifies ±10% per range (±15% on the top range),
   plus a per-range resolution step that dominates at the bottom of a range —
   a 1 uA reading in range 0 is ±30%, not ±10%. Every `measure` result
   carries the resulting bar as `uncertainty.mean_ua_typical` and friends
   (`docs/energy-analysis.md`). Two tools agreeing to three digits would be a
   coincidence, not a proof.
5. **Bandwidth.** If the DUT has content above 50 kHz, every tool reading
   this instrument sees the same folded signal, so they can agree with each
   other and all be wrong together. The tell is that *repeated runs*
   disagree: ripple near a multiple of 100 kHz turns into a DC offset whose
   size is set by the arbitrary phase between the DUT's clock and the sample
   clock. `docs/bandwidth.md` shows the worked case.

## Has any of this been checked against a known current?

Once, on one unit, and only coarsely. A 680 kΩ ±5% resistor across VOUT at
a 3700 mV setpoint should draw 5.4332 uA through that unit's own 1000.625 Ω
range-0 shunt; eleven captures read between 5.5280 and 5.5614 uA, +2.1% from
nominal, all in range 0 with no range switches and nothing saturated.

That rules out a gross error and stops there. The resistor's own ±5%
tolerance puts the true current anywhere in [5.175, 5.719] uA — more than
twice as wide as the 2.1% deviation being checked — so the comparison cannot
resolve the instrument's gain error, and it must not be quoted as if it
could. **No calibrated reference has been used.** `docs/calibration.md`,
"Known-load cross-check", has the arithmetic and what a real check would
take.

## Why is `energy_uj` null?

Because nothing measured the voltage. Energy is `charge x voltage`, and the
PPK2 measures only current.

- **Source Meter mode**: the DUT runs from VOUT, so the configured setpoint
  is a defensible supply voltage and energy is computed (still a setpoint —
  the DUT's terminal sees slightly less after shunt burden and lead drop).
- **Ampere Meter mode**: the DUT runs from its own supply, which the meter
  never sees. The device's VDD field is a leftover setpoint, so energy is
  reported as `null` rather than fabricated.

Pass the real supply voltage explicitly to get energy anyway:

```bash
ppk2lab measure run.ppk2a --assume-voltage-mv 3300 --json
```

The result records `voltage_basis: "caller_override"` and
`voltage_measured: false`, so the assumption travels with the number.

## Why do I see negative current?

Because the conversion subtracts an offset, and near true zero the residual
noise sits on both sides of it. The calibrated value is

```text
x = (adc - O[r]) * ((1.8 / 163840) / R[r])
```

(`docs/calibration.md`), so any ADC code below the range's stored offset
`O[r]` produces a negative current by construction. Measured on one unit
with nothing connected to VOUT, 60 s per run: mean 0.1633 uA with a minimum
of −0.2477 uA, and on a second run mean 0.1769 uA with a minimum of
−0.3356 uA. Negative samples are routine there. That is the shape of a
correctly calibrated zero at a load the instrument can barely resolve, not a
fault. (How large a share of samples went negative was not counted, so no
figure is quoted for it; what was counted is the share below the 200 nA
distribution floor — below.)

ppk2lab never clamps or rectifies, and that is deliberate: half-wave
rectifying symmetric noise inflates the reported mean by roughly the noise
amplitude, which on a sub-microamp measurement is the entire quantity you
were trying to measure. The plausibility filter bounds magnitude only and is
sign-symmetric for the same reason.

One consequence reaches the percentiles. The distribution grid is
logarithmic, so it has no bin for zero or for a negative reading, and its
floor is 200 nA. In those same two runs 69-71% of samples fell below that
floor and `p50` came back as exactly 200 nA. That is an upper bound on the
median, not a measurement of it, so the result names the affected quantiles
in `distribution.quantiles_at_floor`, warns with
`W_BELOW_MEASUREMENT_FLOOR`, and prints them `<=`. Near the noise floor,
read the mean, the minimum, or charge instead — and note that the typical
uncertainty agrees independently: the same runs reported `mean_ua_typical`
0.2177 uA against a 0.17 uA mean, an error bar of ±123% around the reading.
The same unit measuring a 5.55 uA load reported no below-floor samples at
all, so this is a statement about the regime, not about the device.

What a *large or sustained* negative reading does mean is a wiring question,
not a calibration one: check that the DUT's return current actually flows
through the meter, and re-read `voltage_basis` and `energy_note` in the
`measure` output — and see "What is the difference between Ampere and Source
mode?" below.

## What is the difference between Ampere and Source mode?

| | Ampere Meter | Source Meter |
|---|---|---|
| Who powers the DUT | its own supply | the PPK2's VOUT |
| What is measured | current through the meter | current the PPK2 supplies |
| Voltage setting | still feeds the calibration correction term | also sets what the DUT receives |
| Energy | needs `--assume-voltage-mv` | derived from the setpoint |

A common surprise: in Ampere mode the DUT will not run from VOUT. If it is
not powered separately, the capture reads near zero — which is why ppk2lab
warns when DUT power is unknown.

## Why is the mean nothing like the median?

Because a duty-cycled DUT spends most of its *time* in one state and most of
its *charge* in another, and the mean is the charge answer. On the shipped
simulated profile `measure` reports `mean 1806.72 uA` next to
`p50 6.05 uA`: the mean is what drains the battery, the median is the sleep
current, and the DUT never draws 1806.72 uA for a single sample. Both are
correct answers to different questions.

Every result carries `p5`/`p50`/`p90`/`p95`/`p99`/`p999` beside mean/min/max.
`measure --state-threshold 1mA` goes further and costs the two sides
separately — time, mean, charge and run count for each.
`docs/energy-analysis.md` has the worked example.

## Why does `max_current` grow the longer I capture?

Because an unfiltered maximum is a property of the capture, not of the DUT.
Two independent reasons, both of which get worse with length:

1. **Range-switch transients.** The shunt network switches ranges as the
   load moves, and each switch can produce a short transient
   (`docs/calibration.md`). A longer capture contains more switches, so it
   samples more transients, so the largest one it happens to catch drifts
   upward. `--filtered` reports a spike-filtered series alongside the raw
   one — the difference between the two peaks *is* the transient.
2. **Aliasing.** A folded spike lands somewhere in the record at full
   amplitude and the wrong time, and the recorded height depends on the
   sampling phase, so it moves between runs (`docs/bandwidth.md`).

For a decision — a CI threshold, a battery budget — prefer charge, the mean,
or `p99_current` over a stated window. The first two are robust to both
effects; `p99` describes a fraction of the distribution rather than its
extreme, so unlike `max` it does not drift upward as the capture gets longer.
If you need the peak, say over what window and with which filtering, and
expect it to be repeatable only to within the range's accuracy.

## I lost samples on an idle machine. Is that my fault?

Through `0.2.0` it was not yours and not the host's: it was this project's.
The artifact writer compressed each 4 MB chunk on the thread consuming the
sample stream, and the ~290 ms pause overflowed the reader's queue, so a
capture lost a gap at every chunk boundary it crossed. That is why the runs
below each show exactly five gaps over 60 s, and why load made the gaps
*larger* rather than more numerous. Fixed in `0.3.0`;
on the same host, 5 x 30 s and 1 x 60 s captures now record every sample.

The measurements below are kept as the record of that behaviour. Measured
on one host (macOS on Apple silicon, 10 cores) with nothing else running: a
60 s capture lost 25,792 samples — **0.43%** — in 5 gaps of 4,864 to 5,648
samples. Every one was `host_overflow`: the host's bounded stream queue
dropped whole chunks. That is loss on this side of the USB cable, not the
instrument failing to sample.

Loading the machine deliberately (12 CPU spinners plus continuous disk
writes) cost 65,024 samples, 1.08%, in 5 gaps of 9,360 to 23,104. The shape
of the change is the interesting part: the gaps got **larger, not more
numerous**. A third 60 s capture later in the same session lost 304,847
samples (5.1%), so the rate follows whatever else the machine is doing
rather than being a property of the tool or the unit. One host, one unit —
observations, not a figure to plan against.

In all of those captures `timeline.rate_check` stayed `ok` with
`unaccounted_samples_estimate` near −230, and that is the right answer: the
gap table had already accounted for every missing sample, so the wall-clock
witness had nothing to add. It exists for the loss the 6-bit counter cannot
see (below), which this was not.

Nothing here is hidden: the gaps are in the manifest with their positions
and reasons, `charge_is_lower_bound` is set on any window they touch, and
`ppk2lab inspect run.ppk2a --json` lists them without loading a sample. The
only lever measured is host load.

## My capture says `complete: false`. Is the data useless?

No — it is annotated. The samples that arrived are all there; the gaps are
listed with their timeline positions, so nothing is silently compressed.
What you must not do is treat means and integrals over a gappy window as if
they covered the whole window: `charge_is_lower_bound` and
`covered_fraction` exist to say so.

On a **window**, `complete` means gap-free *and* populated end to end. A
10-second `--window` over a 50 ms capture is not complete: it contains no
gaps because it contains almost nothing, and the positions holding neither a
sample nor a recorded gap are counted in `samples.unpopulated`.
`charge_is_lower_bound` covers the same ground plus every other reason a
sample was not counted — an invalid range field, missing calibration, an
implausible value, a sample pinned at the ADC's ceiling.

Exit code 6 means the same thing at the CLI boundary: not a failure, not a
success — data is missing and the caller decides.

## My assertion suite started reporting `incomplete`. What changed?

Nothing about the DUT. Two answers the engine used to give on evidence that
did not support them, it now declines to give:

- **The evaluation window is evaluated as written.** `within 20ms` over a
  capture that ends 5 ms after the anchor used to be trimmed to the capture
  end and reported `passed`. It now reports `incomplete` with
  `reason_code: "window_past_capture_end"`; the other codes are
  `sample_gaps`, `window_unpopulated`, `metric_not_computable`, and
  `metric_at_measurement_floor`. Every
  observation also carries `covered_fraction` and `capture_end_sample`.
- **An anchor is not stitched across a discontinuity.** `after
  uart("TX_DONE")` no longer matches bytes separated by a gap, an
  unsynchronized frame, or a frame error, and `after spi(0x9f, 0x00)` no
  longer matches across two CS transactions. If no occurrence survives, the
  outcome is `no_event` with a warning naming how many were rejected and why
  — which is a different statement from "never present".

`incomplete` exits 6 and `no_event` exits 1; neither is a pass. The usual fix
is to capture further past the anchor, not to widen `within` — widening it
asks a different question.

## What is `timeline_compression`?

The device numbers its samples with a 6-bit counter (0-63), so it can only
describe losses smaller than 64 samples. A larger burst aliases, and a loss
of exactly 64, 128, ... samples is invisible to the counter entirely.

ppk2lab therefore also compares the sample timeline against the host's wall
clock. If a capture advanced far slower than 100 kS/s, samples were lost
however quiet the counter stayed, and the capture is marked incomplete with
`achieved_sample_rate_hz` and `rate_deficit_ratio` reported. Usual causes:
a loaded host, an unpowered USB hub, or a busy USB controller.

Read `achieved_sample_rate_hz` with the capture's length in mind. Measured
on one unit and host, the error in anchoring the first sample to wall time is
a fixed ≈ −2.27 ms, not a drift, so it dominates a short run: the same device
reported 100076 Hz over 3 s and 100004 Hz over 60 s. With that fixed offset
removed, the device clock agreed with the host to within about 10 ppm. A
short capture is not a way to measure the sample rate: 100076 Hz over 3 s is
the anchoring offset, not the crystal.

## Why won't it open my hours-long capture?

Recording an hours-long artifact and reading one whole are different
problems. The write path streams in fixed chunks and its memory does not
grow with duration, so a soak capture records fine; loading one materializes
every sample at once, which is refused above roughly 25 million **stored**
samples — about 250 s at the full rate. The limit counts stored samples, not
elapsed time, so a gappy capture can span far longer and still load.

Three ways through, in the order to try them:

```bash
ppk2lab inspect soak.ppk2a --json              # manifest only; never loads samples
ppk2lab measure soak.ppk2a --window 3600:3660 --json
ppk2lab measure soak.ppk2a --max-samples none --json   # deliberate whole-file load
```

`inspect` answers "was this run any good?" — identity, configuration,
timeline, sample counts, gaps, warnings — without touching a sample. On a
6,000,000-sample artifact it returned in 0.126 s without opening a chunk,
where reading the same file whole peaked at 35.7 MB. Windowed analysis is
the usual answer: a window read peaked at 10.7 MB for a 0.5 s window *and*
for a 5 s one, because the floor is one 1,000,000-sample chunk, which is
CRC-checked whole before it can be sliced. `--max-samples none` is the
escape hatch and it means what it says: budget roughly 8 bytes of RAM per
stored sample.

Decimation (`export --decimate`, `docs/decimation.md`) solves the *file*
size, not the memory: one hour of raw CSV is 18.8 GB at the 52.1 bytes per
sample measured on a real capture, and the same hour at `--bucket-ms 100` is
36,000 rows — but the export still reads the samples it summarizes, so it
obeys the same ceiling. Pair it with `--window`, or with `--max-samples none`
if you have the RAM.

## Can it tell me how long my battery will last?

Not as a command, and the arithmetic is the easy part. Any `measure` result
already carries what the calculation needs:

```text
hours = capacity_mAh / (current_ua.mean / 1000)
```

The reason ppk2lab does not print that number is that four assumptions sit
underneath it, and all four are yours to make:

- the measured window's mean continues indefinitely — true only if the
  window covered a whole number of duty cycles, which the tool cannot know;
- `mean_ua` is the mean over *valid* samples, so a capture with gaps gives
  an answer that is optimistic or pessimistic with no indication which.
  Check `charge_is_lower_bound` and `covered_fraction` first;
- nameplate mAh is not deliverable charge under a pulsed load, to a cutoff
  voltage, or at temperature — for a coin cell driving an RF duty cycle it
  overstates substantially, so the estimate is an upper bound;
- the PPK2 never measures the DUT's terminal voltage, so nothing here
  accounts for the supply sagging as the cell drains.

Quote the charge over a stated window alongside any runtime figure. Charge
is a measurement; runtime is a projection.

## Why does discovery show two ports?

Firmware 1.2.0 and newer expose a second (shell) CDC port. ppk2lab picks the
measurement port by USB interface number where the OS reports one, and
otherwise probes with the read-only metadata command. On macOS, where no
interface numbers are exposed at all, the probe is the normal path — on the
one unit measured there, `discover` reported both ports with
`role: "unknown"` and the probe found the measurement port among them.
Nothing in the port name identifies it, so do not infer the role from the
path.

## `PORT_BUSY` / `PERMISSION_DENIED` — what now?

Follow the `remediation` field; it is platform-specific. On Windows and
macOS this nearly always means another application (usually the nRF Connect
Power Profiler) holds the port exclusively. On Linux it is usually group
membership: add your user to `dialout`/`uucp` or install the udev rule, then
replug. `ppk2lab doctor --json` diagnoses both.

## My UART frames say `unsynced`, or my UART trigger never fires

A receiver cannot know where a frame starts until it has seen a high run
longer than any that can occur inside one. At 8N1 every bit but the start bit
can be high at the same time, so nine bit times of high prove nothing. The
decoder therefore requires **one whole frame time** of continuous idle —
104 samples at 9600 baud — before it will call a falling edge a start bit.
Candidate frames before that are still reported, because they are what the
line did, but they carry `errors: ["unsynced"]` and `confidence: 0.0` and
they never join the byte stream, anchor an assertion, or fire a trigger.

That state is entered at stream start, after every gap, and after every
break. So:

- `ppk2lab capture --trigger 'uart D0 9600 "BOOT"'` joins a line already
  running, every run. A DUT that transmits back to back with no idle
  produces no clean byte and the trigger never fires.
- After a gap, frames up to the next real idle are evidence, not data.

There is deliberately no option to lower the threshold — a shorter
requirement is a supported way to fabricate bytes. If the DUT truly never
idles, put idle between its messages; that is what makes a byte stream
recoverable by any receiver, not just this one. `docs/decoders.md` has the
derivation.

## Can it decode I2C / faster UART / MHz SPI?

Not reliably, and it will say so rather than pretend. At 100 kS/s there are
about 10 samples per bit at 9,600 baud and roughly one at 115,200. The rate
tiers in `docs/decoders.md` are enforced in code: validated rates decode,
conditional rates warn, experimental rates need an explicit opt-in, and
anything beyond is refused. Additional low-speed decoders (bit-banged I2C,
PWM, 1-Wire) are on the roadmap; MHz-class protocols never will be.

"Validated" there names a tier, not a bench result: the boundaries are
samples-per-bit arithmetic and generated-waveform tests. No decoder in this
project has yet read a signal from real hardware, so no decoder error rate
has been measured at any tier.

## Does anything here power my DUT without asking?

No. Enabling DUT power, changing the source voltage, changing mode, and
resetting are the only state-changing operations, they never happen
implicitly, and `configure` is a dry run until `--apply`. You can also set a
hard ceiling for a fragile DUT:

```bash
ppk2lab configure --device S --max-voltage-mv 3600 --voltage-mv 3300 --apply
export PPK2LAB_MAX_VOLTAGE_MV=3600   # or session-wide
```

## I powered the DUT with `configure --apply`, so why did my capture read zero?

Because DUT power does not outlive the process that enabled it. Measured on
one unit, varying only how long the serial port stayed closed between
enabling power and looking again:

| Port closed for | Still powered on reopening |
|---|---|
| 0 ms | 3 of 3 |
| 100 ms | 1 of 3 |
| 250 ms | 2 of 3 |
| 500 ms | 0 of 3 |
| 1000 ms | 0 of 3 |
| 4000 ms | 0 of 3 |

The device de-energizes VOUT once the USB host goes away, deterministically
by half a second. That is a fail-safe in the instrument, not a bug in it.
Mode and source voltage are metadata-backed and do persist; only the output
drops.

So this sequence cannot work, however cleanly each command exits:

```bash
ppk2lab configure --device SERIAL --dut-power on --apply   # warns W_DUT_POWER_TRANSIENT
ppk2lab capture --device SERIAL --duration 10s --output run.ppk2a
```

`configure` reports `dut_power: true` truthfully — and it stops being true
about half a second after the process exits, before `capture` has reopened
the port. `configure --dut-power on --apply` therefore warns
`W_DUT_POWER_TRANSIENT` instead of promising something it cannot keep.

A powered measurement has to happen inside **one** open session, which means
the Python API:

```python
from ppk2lab import PPK2

with PPK2.open(serial_number="SERIAL") as device:
    device.set_dut_power(True)          # explicit, and only here
    result = device.capture(duration_s=10.0)
```

Verify it from the current itself rather than from the return value: this
hardware cannot report its power state back, which is also why an
unpowered DUT in Ampere mode reads near zero instead of raising anything.

## How do I capture a cold-boot inrush?

The interesting part of an inrush is the moment power arrives, so the capture
has to be running before it does. Pass `at=` a list of
`(delay_s, callable[, label])`: each fires between two sample blocks, on the
capture's own thread, at a sample index the capture then records.

```python
with PPK2.open(serial_number="SERIAL") as device:
    device.set_dut_power(False)                       # start from cold
    result = device.capture(
        duration_s=10.0,
        output="inrush.ppk2a",
        at=[(2.0, lambda: device.set_dut_power(True), "dut_power_on")],
    )
print(result.scheduled_actions)
# [{'label': 'dut_power_on', 'requested_s': 2.0, 'fired_index': 200192,
#   'fired_s': 2.0019, 'error': None}]
```

`fired_index` is where it actually happened, so the moment is locatable in the
samples rather than inferred from a wall clock. A timer thread would land
within a scheduler quantum and would be writing to the same serial port the
reader is draining; this does neither. If the callable raises, the capture
continues, the failure is recorded against the action, and `W_SCHEDULED_ACTION`
says so — samples already taken are not worth losing to a bad callback.

**`at=` cannot be combined with a trigger**, and asking for both is a usage
error rather than a best-effort attempt. A triggered capture's timeline starts
`pre` samples *before* the trigger fires, so a delay measured from the first
sample cannot be honoured, and the `fired_index` recorded against the action
would predate the action itself. Schedule the stimulus on a `duration_s=`
capture, or perform it before a triggered capture starts.

An action whose delay never came due is recorded too: the same entry, with
`fired_index: null`, `fired_s: null`, and an `error` naming where the capture
ended — plus one `W_SCHEDULED_ACTION` for the whole set. Without that record a
capture whose stimulus never happened was byte-for-byte indistinguishable from
one that scheduled nothing, which is the case where the samples look like a
perfectly good measurement of the wrong thing.

**Both the `at=` callables and `on_progress` run on the thread consuming
samples.** That is the point for `at=` — the action lands between two sample
blocks, at an index the capture can record — and the cost is that a slow
callable stalls the consumer. The reader buffers up to `queue_bytes` (4 MB by
default) as a back-pressure target, and the device sends 100,000 samples/s x
4 bytes = 400 kB/s, so that buffer is about ten seconds of stream: a callable
that returns well inside that costs nothing, and one that does not drops
samples. It is the same class of stall that produced a `host_overflow` gap at
every artifact chunk boundary until the `0.3.0` fix (see "I lost samples on
an idle machine"). Do the slow part after the capture returns.

`on_progress` takes the same care but is cheaper to get right: it is called at
most once every 0.25 s with `{stored, elapsed_s, gap_count, sample_limit}`, it
now also reports while a triggered capture is waiting to fire, and a callback
that raises is disabled once with `W_PROGRESS_CALLBACK` instead of ending the
capture.

There is deliberately no CLI flag for this. `capture` never enables DUT power
and has no option that would; scheduling one is an explicit, Python-only act.

## How do I try it without hardware?

Add `--simulate` to any command. It runs a built-in simulated PPK2 with a
repeating activity cycle (current burst, UART traffic, an SPI transaction, a
square wave). It exercises the toolchain end to end — but simulated numbers
are not measurements, and results say `simulated: true`.
