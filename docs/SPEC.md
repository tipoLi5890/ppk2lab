# Public data model, states, API, and schema contracts

Status: frozen since `0.2.0`; `0.4.0` is the current release and adds to that
surface without changing it. Changes follow the stability policy at the end of
this file. `SCHEMA_VERSION` is `"1"` and is independent of the package
version.

## Core concepts

### Timeline

Both current and D0-D7 are sampled at a fixed 100 kS/s (one sample per
10 us) on one synchronized timeline. A **timeline index** counts sample
periods since the capture start, *including* missing samples: a gap advances
the timeline without storing data, so `time_s = index * 1e-5` stays truthful
and later data is never shifted earlier.

### GapEvent

Every loss of samples is an explicit event:

```json
{"index": 1200, "missing": 7, "reason": "counter_skip", "ambiguous": true}
```

- `missing` is `null` when the count is unknown (the timeline is then marked
  degraded and the capture incomplete).
- `ambiguous` is true when the count came from the 6-bit counter and could be
  larger by a multiple of 64.
- Reasons: `counter_skip` (device-side), `host_overflow` (bounded queue
  dropped chunks; exact byte count converted to samples),
  `discontinuous_feed` (decoder fed non-contiguous data), `usb_stall`,
  `stream_desync` (byte framing lost and re-aligned), `sample_gap` (a
  decoder annotation marking the samples a gap removed, so a gap between
  frames is visible in decoded output). The list is an **open** catalog
  published by `capabilities --json` as `gap_reasons`, each entry
  `{code, category, meaning}`; a new reason is a new way samples can be
  lost, not a breaking change.
- Only `host_overflow` has been seen on hardware so far: in three 60 s
  captures every gap came from the host's bounded queue dropping whole chunks,
  and under load those gaps grew larger rather than more numerous
  (docs/protocol-spec.md). The other reasons are tested against the mock
  transport, not witnessed.

### Device state

`DeviceState` fields are `None` when unknown; unknown is preserved, never
defaulted: `mode` (`ampere`/`source`), `source_voltage_mv`, `dut_power`,
`measuring`, plus `source_voltage_basis` recording how the voltage was
learned (`configured_source` beats `device_metadata`, which is only a
regulator setpoint).

`DeviceState` is frozen and `PPK2.state` is read-only: it is updated only by
the documented state-changing methods, each of which has already put its
command on the wire. The reason is `dut_power`, which the device cannot
report back — a host-side assignment would suppress `W_DUT_POWER_UNKNOWN`
and write `dut_power: true` into a capture manifest as a hardware claim
nobody commanded. Direct assignment raises.

### StateChange

Every state-changing operation returns:

```json
{
  "operation": "set_source_voltage_mv",
  "requested": {"voltage_mv": 3300},
  "before": {...}, "after": {...},
  "applied": true,
  "observed_after": true,
  "warnings": []
}
```

`observed_after=false` means the "after" state is the requested state, not a
device readback (DUT power is in this category — metadata cannot report it).

DUT power is also the one state that does not persist beyond the session. The
device de-energizes VOUT once the host closes the port: measured by varying
only how long the port stayed closed, the output was still live in 3 of 3
trials at 0 ms, was a coin flip between 100 and 250 ms, and was off in every
trial at 500 ms and beyond. A powered measurement therefore has to happen
inside one open session, and `configure --dut-power on --apply` warns
`W_DUT_POWER_TRANSIENT` rather than implying otherwise. Mode and source
voltage are metadata-backed and survive. This is why the fail-safe on close
costs nothing: the hardware was going to drop the output anyway.

### Concurrency

One `PPK2` handle owns one serial port and is **not** safe to use from two
threads at once. The stream claim is the only part that is synchronized, and
it exists to refuse a second reader rather than to allow one: the 4-byte
sample words carry no sync word, so two readers taking turns on one port
would split words between them and neither would notice. A second `stream()`
on a handle that already has one open is refused with `UsageError`, and the
claim is held until that stream's teardown is over — the reader stopped, the
measurement stopped, the port drained — not merely until it stops yielding. A
handle that reads as free is one whose port is quiet.

`AsyncPPK2` does not change this. It runs each blocking call on a worker
thread through `asyncio.to_thread`, so concurrency is between an awaiting
coroutine and one worker, never between two callers of the same handle.
Cancellation is safe but not instantaneous — `asyncio.to_thread` cannot
interrupt a worker mid-call, so a cancelled `open()` still finishes opening
the port (and then closes it), and a cancelled `capture()` runs to its
stopping condition. Use the stream iterator's context manager rather than
cancelling around a bare `async for`.

Abandoning a stream part-way through its `with` block releases the claim, and
the same handle captures again immediately — confirmed on hardware. That is
worth confirming rather than assuming, because the failure the claim guards
against, two readers splitting 4-byte words between them, raises nothing of
its own.

Separate devices are independent: two `PPK2` handles on two units share no
state and may be used from two threads or two processes, one handle each. This
has not been exercised on hardware: every session so far has had one unit
attached.

### Multiple devices

`discover()` returns every attached unit and `--device SERIAL` selects one.
What the project does **not** provide is a common time base. Each unit
free-runs its own 100 kS/s clock; nothing synchronizes them, and the only
shared reference is each capture's `first_sample_utc`.

How good that reference is has been measured only indirectly, on one unit and
one host. Across three captures the wall-clock interval between the first and
last sample anchors fell short of the device's own sample count by a **fixed
≈ −2.27 ms** — the same at 3 s and at 60 s, so the anchoring error is an offset
and not a proportional drift, and with it removed the device clock agreed with
this host to within ~10 ppm. The absolute accuracy of `first_sample_utc`
against true UTC is a different question and has never been measured; that
needs an external time reference. What the measurement does establish is the
scale: the anchoring error is milliseconds, not microseconds.

`anchor_uncertainty_s` is **not** that bound. It reports the span of the first
delivered block — 0.00016 s in those captures, an order of magnitude smaller
than the offset above — and is a statement about how late the anchor stamp can
be relative to the samples it marks, nothing more. Do not substitute it.

Two captures can therefore be put on a common time base at the millisecond
scale and no better, and their sample indexes cannot be compared at all. A
two-rail measurement that needs sample-accurate alignment is outside what this
hardware can support through this tool. Two units have never been run
together, so even that bound is reasoned from single-unit numbers.

### Interrupted sessions

A PPK2 left streaming by a process that exited without stopping it keeps
sending samples into the port's buffer, and the next session's first read
would parse that residue as its own data. `PPK2.open()` therefore performs
recovery before reading metadata: it sends the stop command, drains whatever
is queued, and records the byte count. `doctor` reports it as
`session_recovery`, and a recovered session raises `W_SESSION_RECOVERED`.
`PPK2.recover_session()` is the same routine exposed for a caller who needs to
run it again mid-session; `open()` has already done it once.

Confirmed on hardware in two ways. After a `SIGKILL` mid-capture — where the
process gets no chance to stop the stream — the next open discarded **17,412
stale stream bytes** and then read metadata cleanly, with `doctor` reporting
`session_recovery` as a warning carrying that exact count. After a `SIGTERM`
the capture was preserved instead: a readable artifact holding 357,888
samples, exit 6, and no orphan temp file left behind.

One caveat a reader of the interruption catalog will meet: the SIGTERM case is
recorded as `interruption.reason = "keyboard_interrupt"`. SIGTERM does not
raise on its own, so the capture runner installs a handler that turns it into
a `KeyboardInterrupt` — without which a terminated capture would leave a
half-written temp file and no manifest — and the reason string does not
distinguish the two. A SIGTERM from a process manager is not an operator at a
keyboard. This is a known imprecision, not a subtlety: do not read that reason
as evidence a human interrupted the run.

## Python API surface (sync)

```python
import ppk2lab

ppk2lab.discover()                       # -> list[DeviceInfo], read-only
dev = ppk2lab.PPK2.open(serial_number=..., port=..., simulate=False)
dev.refresh_metadata()                   # read-only (opcode 0x19)
dev.set_mode(ppk2lab.Mode.SOURCE, dry_run=True)   # -> StateChange
dev.set_source_voltage_mv(3300)          # validated 800-5000 mV
dev.set_dut_power(True)                  # never called implicitly
dev.reset()
with dev.stream(duration_s=..., sample_limit=...) as events:   # SampleBlock | GapEvent
    for event in events:
        ...
result = dev.capture(duration_s=5.0, output="run.ppk2a")  # -> CaptureResult
dev.close()                              # restores session-start power state
```

`simulate` decides explicitly when it is given and reads `PPK2LAB_SIMULATE`
when it is not, in `PPK2.open` and in `discover` alike; `PPK2LAB_SIMULATE=1` is
what makes the environment variable interchangeable with `--simulate`. It never
applies to an injected `transport=`, because the environment must not decide
the identity of a transport the caller supplied: a real instrument would be
recorded as `simulated: true` in a stored manifest while every byte still
reached the wire. Only an explicit `simulate=True` labels an injected transport
simulated.

`with dev.stream(...) as events:` is the documented idiom. A stream owns the
instrument — the claim is taken when `stream()` returns, not at the first
iteration, so an iterator that is created and never started still holds it —
and releasing it must not depend on when the iterator happens to be
collected. A bare `for event in dev.stream(...)` still works.

`ppk2lab.AsyncPPK2` mirrors the same surface with `async` methods, and its
`stream()` returns an `AsyncStreamIterator`:

```python
async with adev.stream(duration_s=1.0) as events:
    async for event in events:
        ...
```

`async for event in adev.stream(...)` and `contextlib.aclosing()` are
unchanged; the context manager is the requirement rather than a nicety
because an abandoned async generator holds the device claim until the event
loop finalizes it. Cancelling an `await` on `AsyncPPK2.capture()` does not
stop the capture: `asyncio.to_thread` cannot interrupt its worker, so bound a
capture with `duration_s`/`sample_limit` rather than with a cancellation.

Offline:

```python
cap = ppk2lab.Capture.load("run.ppk2a")
anns = ppk2lab.decode_capture(cap, ppk2lab.UARTDecoder(rx="D0", baud=9600))
from ppk2lab.analysis import measure_window, measure_annotations, parse_rule, evaluate_assertion
from ppk2lab.analysis import compare_stats, summarize_side, dominant_range
```

`compare_stats(a, b, *, metric=, same_instrument=)` differences two
`WindowStats` as `b - a` and is what `ppk2lab compare` publishes; see
"Differencing two captures" below for what its error bar assumes.

## CLI JSON envelope

Every command with `--json` emits (schema `envelope`):

```json
{
  "schema_version": "1",
  "command": "capture",
  "ok": true,
  "result": {},
  "warnings": [],
  "error": null
}
```

`error` (schema `error`) always carries `code`, `message`, `remediation`,
`exit_code`. Codes are frozen strings (see `ppk2lab capabilities --json`,
`error_codes`).

`warnings` is an array of `{code, message, category}` for the same reason: a
program deciding whether to retry cannot parse prose. Codes live in
`ppk2lab.diagnostics` (`W_SAMPLE_GAPS`, `W_TIMELINE_COMPRESSION`,
`W_VOLTAGE_ASSUMED`, `W_NOT_CALIBRATED`, …); new codes may be added, and an
existing code never changes meaning within a schema version.

`category` says which part of a result the warning is about — `capture
integrity`, `measurement trust`, `device state`, `analysis` — so a consumer can
route a warning without joining against the catalog first. It is **not** a
severity: `W_DUT_POWER_UNKNOWN` and `W_SAMPLE_GAPS` differ in what they are
about, not in how loud they are, and nothing in this project ranks them. It is
`null` for a code the reading version does not know, which is the honest answer
for an open catalog. The three keys are the shape everywhere a warning is
published: a live `--json` result, the `warnings` array stored in a capture
manifest, and `inspect` output — an artifact written before `category` existed
has it supplied on the way out rather than being republished in the old shape,
so one reader handles every artifact.

### Voltage and energy

Results that carry energy also carry `voltage_measured: false` and a
`voltage_basis`, because the PPK2 measures current only — every energy
figure is charge times an assumption. In Ampere Meter mode the assumption is
not defensible and `energy_uj` is `null` unless the caller supplies the DUT's
real supply voltage. See docs/energy-analysis.md.

Even in Source Meter mode the setpoint is not the DUT's terminal voltage: the
current passes through the selected shunt, and that burden is not measured.
A measured instance — 5.55 µA through a 1000.625 Ω range-0 shunt at a 3700 mV
setpoint — drops 5.44 mV, so the load saw 3.6946 V, 0.147% low. How
large the burden gets depends on the range in use and the current through it,
and nothing in the artifact records it; the tool never corrects for it, and
`energy_note` says so.

### Distribution statistics

Window results carry `current_ua.p5/p50/p90/p95/p99/p999` alongside
mean/min/max, and a `distribution` block naming the grid they came from — `p5`
and `p95`, the conventional floor and burst statistics in power work, joined the
set in `0.3.0`. The set is fixed rather than an arbitrary `pN`, which is what
lets every quantile be collected unconditionally. They are computed
from a fixed log-spaced histogram (200 nA to 1 A, 128 bins per decade)
accumulated in the same pass as everything else, so they cost no extra memory
and are available for an hours-long capture. Consequences a consumer must
know:

- A quantile is **quantized to the grid**: it carries up to ±0.90% of
  relative error on top of the instrument's own. That is about a tenth of
  Nordic's typical ±10%, so it never decides whether a quantile is usable.
- A quantile is clamped into the observed `[min, max]` and is never a value
  outside what was measured. The clamp does **not** rescue a quantile that
  fell in the floor bin, except in the one case where the whole distribution
  sits below the floor: it is the maximum that pulls a value down, so a window
  holding anything above the floor leaves the floor-bin quantile at the floor.
- Readings at or below 200 nA — including zero and negative ones — have no
  bin. They are counted in `distribution.below_grid_samples`, and a quantile
  served from them is reported at the grid floor, which is an **upper bound**,
  not an estimate. Every affected quantile is named in
  `distribution.quantiles_at_floor`, the result carries
  `W_BELOW_MEASUREMENT_FLOOR`, and human output prints the value with a `<=`
  sign. This is the ordinary case at the bottom of range 0, not an edge case:
  on an unloaded PPK2 measured over 60 s, 69-71% of samples fell below the
  floor and `p50` came back as exactly 200 nA. Use the mean, the minimum, or
  charge in that regime — they are computed from the samples, not from the
  grid.
- `state_split` is `null` unless a threshold was supplied. When present it
  carries `threshold_ua` with it, because the split is a function of the
  threshold and is unreadable without it. `runs` counts runs of consecutive
  *stored* samples in a state; a gap ends a run rather than bridging it.

Quantiles are collected unconditionally, so a capture's own recorded
statistics and a later `measure` of the same window always agree — including
the sub-window batching behind `mean_ua_batch_stderr`, which is keyed on the
valid-sample count and not on how the samples happened to arrive.

### Measurement uncertainty

Every window result carries an `uncertainty` block. It reports two different
quantities that must never be added together or confused for each other.

**1. Typical per-range uncertainty (systematic).** Nordic publishes a typical
accuracy and a typical resolution per shunt range (transcribed in
docs/calibration.md, sourced in docs/sources.md). ppk2lab combines them as

```
charge_uncertainty = Σ_r ( accuracy_r · |charge_r| + resolution_r · n_r · 10 µs )
mean_uncertainty   = charge_uncertainty / (valid_samples · 10 µs)
energy_uncertainty = |energy| · charge_uncertainty / |charge|
```

with `accuracy = (0.10, 0.10, 0.10, 0.10, 0.15)` and
`resolution_µA = (0.2, 0.5, 5.0, 50.0, 1000.0)`, and `charge_r` / `n_r` the
per-range decomposition already reported in `charge_per_range_uc` and
`samples_per_range`.

Three properties of that formula are load-bearing:

- **Summed linearly, never in quadrature and never divided by √N.** These are
  *gain* specifications. Within a range the error is the same multiplicative
  error on every sample taken in it — fully correlated — so it does not
  partially cancel and it does not average away. An error bar that shrank as
  the capture got longer would be describing a random error the instrument
  does not have.
- **The resolution term is required, not decorative.** A purely multiplicative
  model understates the error at the bottom of a range, where the absolute
  step size dominates: a 1 µA sleep current in range 0 is ±0.1 µA of gain plus
  ±0.2 µA of resolution, so ±30%, not ±10%. It is summed linearly too, because
  an unmeasured offset of up to one resolution step is the same offset on
  every sample.
- **These are *typical*, not guaranteed.** Nordic publishes them that way.
  Every field is named `..._typical...`, and `uncertainty.guaranteed` is
  always `false`. Treating them as limits would be a claim the project cannot
  source.

**2. Statistical uncertainty of the mean.** `mean_ua_batch_stderr` is a
separate, separately-labelled quantity: how repeatable this particular mean
is, estimated from the spread of ~20-40 sub-window batch means. It is *not*
σ/√N over samples. Samples 10 µs apart through a shunt-switching front end are
heavily autocorrelated — a duty-cycled load holds one level for thousands of
them — so σ/√N would report an interval an order of magnitude too small. On a
strictly periodic load whose batches do not contain whole periods, the batch
estimate is conservative; it is never optimistic. It says nothing about
whether the instrument is reading *true*, only about how much this mean would
move if the window moved.

**Reporting.** JSON keeps full precision. Human-readable output is rounded to
the digits the interval supports — `1810 ± 190 µA`, not
`1806.719544039553` — with the uncertainty at two significant figures and the
value at the same decimal place
(`ppk2lab.capture.stats.format_with_uncertainty`).

**A worked figure, so the order of magnitude is not guessed at.** A 142 µA
mean sits in range 1, where this model gives `0.10 × 142 + 0.5 = 14.7 µA`,
i.e. **142 ± 15 µA**. Any tighter interval — a percent-level bar on the same
reading — is not derivable from anything Nordic publishes; the only route
from ±10% to ±1.5% is dividing by √N, which is exactly the invalid step
described above. docs/faq.md's ±10% is the correct order.

**What would make this better, and what it would take.** A per-unit *measured*
error bar needs a known-load cross-check against a calibrated reference at
several points inside each range, on each unit, recorded with its firmware
fingerprint. Until that exists the project publishes the vendor's typical
figures and says so; it does not invent a tighter number.

One cross-check has been run, and it is worth stating exactly what it did and
did not settle. A 680 kΩ ±5% resistor between VOUT and GND at the unit's
3700 mV setpoint should draw `3.700 / (680000 + 1000.625) = 5.4332 µA` as a series
circuit through that unit's own R0; eleven captures read 5.5280-5.5614 µA,
i.e. 5.55 µA, **+2.1%** from nominal. The resistor's own ±5% tolerance puts
the true current anywhere in `[5.175, 5.719] µA`, which contains both the
nominal value and the reading. So the check confirms there is **no gross
error** — and that is all it can do. It cannot resolve the instrument's own
gain error, and reporting +2.1% as if it were a measured accuracy would be
exactly the substitution this section exists to refuse. Resolving gain needs a
resistor an order of magnitude tighter, or a calibrated reference.

### Differencing two captures

`ppk2lab compare` (`compare_stats`) reports `b - a` for one metric and prices
the difference with the same per-range typical figures. The bar is tighter than
the two absolute ones only when a premise holds: both captures stayed in one
shunt range **and** came from one instrument. The gain error is one physical
unit's residual, so it scales the difference rather than each reading — but two
units carry independent residuals, and the same range index on a second unit is
not the same shunt. `same_instrument` records which case applied: `true` from
matching serial numbers, `false` from differing ones — `W_INSTRUMENT_MISMATCH`,
and the cancellation is withdrawn — and `null` when the captures did not
identify their instruments, where the cancellation is kept and the note says
the premise is unverified. Unknown is not the same as different.

"Stayed in one range" is judged on the sample count **and** on absolute charge,
both at 99.5%. The metrics being differenced are charge-weighted while a sample
count is not, and a duty-cycled load can hold almost all of its samples in one
range and almost all of its charge in another; pricing that with the wrong
shunt's accuracy publishes a sentence about the hardware that is not true.

Two more properties of the reported difference:

- `relative` divides by `abs(a_value)`, so its sign always matches the delta's.
  This instrument legitimately reads below zero on an unloaded input, and a
  signed denominator turned a rise against a negative baseline into a reported
  fall.
- An `energy` comparison also publishes a `voltage` block — each side's
  setpoint, basis and note, and whether the two differ. Energy is charge times
  an assumed supply, so two captures taken at different setpoints differ by the
  instrument's configuration as well as by the DUT; `W_VOLTAGE_ASSUMED` says so
  rather than letting the difference read as the DUT's.

## Exit codes (frozen)

| code | meaning |
|---|---|
| 0 | success |
| 1 | assertion failed |
| 2 | usage / schema / invalid input |
| 3 | device not found |
| 4 | permission denied or port busy |
| 5 | protocol / firmware / metadata mismatch |
| 6 | capture incomplete (data loss) |
| 7 | optional capability missing |
| 8 | unsafe state change refused |
| 9 | internal error |

Exit 4 also covers a transport failure and a stalled stream
(`TRANSPORT_ERROR`, `STREAM_STALLED`): all four are "the host cannot talk to
this device right now". `capabilities --json` (`error_codes`) maps every
error code to its exit code.

## Schemas

`ppk2lab schema --list` enumerates all published schemas; `ppk2lab schema
<name>` prints one. They are generated from the same definitions the code
uses (`ppk2lab.schemas`), and tests validate live CLI output against them.

## Stability policy

- Adding fields to results is backward compatible; removing or renaming
  fields, error codes, or exit codes requires a `SCHEMA_VERSION` bump and a
  CHANGELOG migration note.
- The warning, gap-reason and interruption-reason catalogs are **open**:
  adding a code is not a breaking change, and a reader must treat an
  unfamiliar one as unknown rather than as invalid. An existing code never
  changes meaning within a schema version.
- A value that changes because it was affirmatively wrong is a bug fix, not a
  change of meaning, and does not bump `SCHEMA_VERSION` — but it does need a
  CHANGELOG entry under behaviour changes, because a result that used to read
  as a pass can start reading as "cannot be evaluated".
- **Widening a field's type** — most often making it nullable, because a
  situation turned up in which the honest answer is "no value" rather than an
  invented one — sits between the two rules above. It does not bump
  `SCHEMA_VERSION` when the field was introduced in the same release, since no
  published version ever promised the narrower type. Widening a field a
  released version already published *does* bump it: a consumer that validated
  against the old schema, or that indexed the field without a null check, was
  entitled to rely on what shipped. Either way it needs a CHANGELOG entry
  saying which field and why the wider type is the truthful one. Narrowing a
  type is a removal and follows the first rule.
- The capture `format_version` (currently 1) is append-only: newer readers
  open older files; older readers refuse newer files explicitly.
- Derived exports (CSV/VCD/JSONL) are reproducible views over raw captures
  and never replace them.
