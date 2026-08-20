# Configure and capture

`configure --apply` is state-changing; `capture` is measurement-only and
never enables DUT power.

## Inputs to collect before touching hardware

Mode (`ampere`/`source`); for source mode the explicit `--voltage-mv`
(stop and ask if the user didn't provide it); whether the PPK2 powers the
DUT (only on explicit request); duration and output path.

## One-shot capture

```bash
ppk2lab discover --json                                   # pick the serial
ppk2lab configure --device S --mode source --voltage-mv 3300 --json   # dry run, show it
ppk2lab configure --device S --mode source --voltage-mv 3300 --apply --json
ppk2lab capture --device S --duration 5s --output run.ppk2a --json
```

Mode and source voltage survive this split across two processes: they are
metadata-backed and were still in place after the port closed and reopened.
**DUT power is not.** So this shape works when the DUT is powered from its
own supply or from VOUT held on by something else; read the next section
before adding `--dut-power on` to it.

Durations take `us`, `ms`, `s`, `min`, `h` — `--duration 8h` is a soak run,
not a typo. An output path that is a directory is refused before the first
sample is recorded, not after the whole capture.

Verify each configure change's `after` state; relay any
`observed_after: false` (DUT power has no readback). Report from the
capture result: `complete`, stored/missing samples, mean/peak current,
charge/energy, path, `capture_sha256`.

`--tag KEY=VALUE` (repeatable) records what the capture is *of* — board
serial, firmware build, experiment id — inside the artifact. It comes back
verbatim as `user_tags` on the capture result itself, so a script that just
tagged a run does not have to reopen the file to read them back, and
`inspect --json` returns the same map later. Nothing interprets a tag;
values are strings and are never coerced.

`complete: false` (exit 6): data is preserved with loss markers — report
the gap table and interruption reason; offer a retry with lower system
load or shorter duration. On session close the library restores the
starting power state; relay restoration warnings.

## Powering the DUT: one session, not two commands

The instrument de-energizes VOUT once the USB host goes away. Measured by
varying only how long the serial port stayed closed between enabling power
and looking: still on in 3 of 3 trials at 0 ms, 1 of 3 at 100 ms, 2 of 3 at
250 ms, and 0 of 3 at 500 ms, 1 s and 4 s. Deterministically off by half a
second.

So the sequence `configure --dut-power on --apply` then `capture` does not
work, and it does not fail loudly either: `configure` exits 0 and reports
`applied: true` (with `W_DUT_POWER_TRANSIENT`), the capture then runs
against an unpowered DUT and returns a near-zero mean that reads like a
good sleep-current result. Treat any near-zero capture as unpowered until
proven otherwise.

A powered measurement has to stay inside one open session, which means the
Python API:

```python
from ppk2lab import PPK2, Mode

with PPK2.open(serial_number="S") as dev:      # or simulate=True
    dev.set_mode(Mode.SOURCE)
    dev.set_source_voltage_mv(3300)
    dev.set_dut_power(True)                    # only on explicit request
    result = dev.capture(duration_s=5.0, output="run.ppk2a")
print(result.stats.mean_ua, result.complete)
```

The session restores the power state it found on close. There is no
readback for DUT power on this hardware, so the measured current is the
only verification available: a plausible non-zero draw is the evidence
that power was on, and nothing else is.

## Scheduled stimulus and live progress (Python only)

Neither has a CLI flag, and `at=` deliberately does not: `capture` never
enables DUT power and has no option that would, so scheduling something
that does is an explicit Python act.

```python
result = dev.capture(
    duration_s=10.0,
    output="inrush.ppk2a",
    at=[(2.0, lambda: dev.set_dut_power(True), "dut_power_on")],
    on_progress=lambda p: print(p["stored"], p["gap_count"]),
)
```

- Each `at=` entry is `(delay_s, callable[, label])`, measured from the
  first sample, and fires *between* two sample blocks at a sample index the
  manifest records (`scheduled_actions[].fired_index`). A `delay_s` that is
  negative, NaN or infinite is a usage error before the capture starts.
- **`at=` cannot be combined with a trigger** — `UsageError`
  (`INVALID_ARGUMENT`), raised before the capture starts rather than
  attempted. A triggered capture's timeline begins `pre_samples` before the
  trigger fires, so a delay measured from the first sample cannot be
  honoured and the recorded `fired_index` would predate the action. Run the
  stimulus before the capture, or use `duration_s=`/`sample_limit=`.
- **An action that never came due is still recorded** — same entry, with
  `fired_index: null`, `fired_s: null` and an `error` saying when the
  capture ended — plus one `W_SCHEDULED_ACTION` for the whole set. So "the
  stimulus never happened" is visible in the artifact instead of looking
  like a capture that scheduled nothing. Check it before reporting an
  inrush that is not in the samples.
- If the callable raises, the capture continues, the failure lands in that
  entry's `error`, and `W_SCHEDULED_ACTION` says so. Samples already taken
  are not worth losing to a bad callback.
- `on_progress` is called at most every 0.25 s with
  `{stored, elapsed_s, gap_count, sample_limit}`, and now also **during a
  trigger wait** — which is the 90 s unattended capture the callback exists
  for. A callback that raises is disabled once, with
  `W_PROGRESS_CALLBACK`; the capture continues.
- **Both callables run on the thread consuming samples.** That thread
  drains a queue budgeted at `queue_bytes` (4 MB by default) — at 100 kS/s
  x 4 bytes, about ten seconds of stream — so a callable returning well
  inside that costs nothing, and one that does not drops samples.
  Stalling that thread is exactly the fault that produced a gap at every
  chunk boundary through `0.2.0` (below). Do the slow part afterwards.

## The timeline block

Also read the `timeline` block. The device's 6-bit counter can only describe
losses under 64 samples (a loss of exactly k*64 is invisible to it), so each
capture is cross-checked against the wall clock:

- `rate_check: "ok"` — the timeline advanced at roughly 100 kS/s;
- `"deficit"` — it ran more than 10% short, so samples were lost beyond
  what the counter reports; `achieved_sample_rate_hz` and
  `rate_deficit_ratio` quantify it. Usual causes: a loaded host, an
  unpowered USB hub, a busy USB controller;
- `"too_short"` / `"not_applicable"` — the check could not run (a capture
  under two seconds, or a triggered capture whose pre-trigger buffer makes
  the comparison meaningless).

The block also carries `first_sample_utc`, `anchor_uncertainty_s`, a signed
`rate_offset_ratio`, and `unaccounted_samples_estimate` against
`unaccounted_floor_samples`. A deficit smaller than the floor is inside
what host anchoring and clock tolerance can explain; above it, the loss is
witnessed and shows up as `W_UNACCOUNTED_SAMPLES`.

What one hardware session established about those fields:

- The anchoring error was a **fixed ≈ −2.27 ms** — −2.290 ms over 3 s,
  −2.253 ms over 60 s, −2.26 ms over 60 s under load. An offset, not a
  drift. With it removed the device clock agreed with that host to within
  about 10 ppm.
- Therefore `achieved_sample_rate_hz` read 100076 Hz on a 3 s capture and
  100004 Hz on a 60 s one. The short figure is the fixed offset spread over
  a short window: **a short capture is not a way to measure the sample
  rate.**
- `anchor_uncertainty_s` reported 0.00016 s. That is the first block's
  span, which is what the field documents — a different quantity from the
  end-to-end offset above, and not a substitute for it.
- `unaccounted_floor_samples` was 10,182 (3 s) and 13,032 (60 s), dominated
  by an **assumed** anchoring jitter of 0.05 s. Nothing was tightened: one
  unit on one host is not grounds for narrowing a safety floor. Say the
  jitter is an assumption whenever a conclusion rests on it.

## How much loss to expect

Little to none. The table below is the `0.2.0` behaviour and is kept because
it is what the bug looked like: the artifact writer compressed each 4 MB
chunk on the thread consuming samples, so a 60 s capture — five chunk
boundaries crossed — lost exactly five gaps, and host load lengthened each
stall rather than adding gaps. Fixed in `0.3.0`; on the same host, 5 x 30 s
and 1 x 60 s captures now record every sample. Measured over 60 s at
100 kS/s on one macOS host, before the fix:

| host state | missing | share | gaps | gap sizes (samples) |
|---|---|---|---|---|
| idle | 25,792 | 0.43% | 5 | 4864, 5008, 5136, 5136, 5648 |
| 12 CPU spinners + continuous disk writes | 65,024 | 1.08% | 5 | 9360, 10288, 10960, 11312, 23104 |

A third 60 s capture later in the same session lost 5.1%, so the rate
follows whatever else the machine is doing rather than a fixed budget.
Every gap was `host_overflow` — the host's bounded queue dropped whole
chunks — and under load the gaps got **larger, not more numerous**. That is
the signature to expect; a `counter_skip`, `stream_desync` or `usb_stall`
reason points at the cable, hub, or controller instead (diagnostics.md).

In all of these `rate_check` stayed `ok` with `unaccounted_samples_estimate`
near −230, which is the correct result: the gap table already accounted for
the loss, so the wall-clock witness had nothing to add. It exists for loss
the 6-bit counter cannot see, and this was not that.

Report loss rather than retry blindly: a rerun on a busy host will lose
more. Loss at this scale already has consequences downstream — a capture
missing about 1% came back `incomplete` from `assert`, with
`covered_fraction: 0.9892` (regression.md).

Without `--output` the capture is held in RAM and is refused beyond 60 s;
pass `--output` (preferred) or `--in-memory` if buffering is genuinely
intended.

## Look before you load

`ppk2lab inspect run.ppk2a --json` reads the manifest and never touches a
sample chunk: identity, duration, gap count, calibration, firmware
fingerprint, interruption, and the statistics the capture recorded at write
time. Measured at 0.126 s on a 6,000,000-sample artifact, with no chunk
opened. Use it first on any artifact you did not just create, and on every
artifact from a long run.

Everything else (`decode`, `measure`, `assert`, `export`) materializes
samples and is capped by `--max-samples`, default ~25 M — about 250 s at
100 kS/s, roughly 200 MB of RAM. A longer capture is **not** corrupt; it
exits 2 with `CAPTURE_TOO_LARGE`, and the remediation names the ways
forward. In order of preference:

```bash
ppk2lab inspect soak.ppk2a --json                        # manifest only
ppk2lab measure soak.ppk2a --window 3600:3660 --json     # seconds; reads only those chunks
ppk2lab export soak.ppk2a --format csv --output slice.csv --window 3600:3660
ppk2lab measure soak.ppk2a --max-samples none --json     # last resort: ~8 bytes/sample of RAM
```

A window reads only the chunks it falls in, so peak memory follows the
window, not the file — with a floor of one 1 M-sample chunk, which is
CRC-checked whole before it can be sliced. Measured: peak 10.7 MB for a
0.5 s window *and* for a 5 s window, flat, because both sit inside that
floor. A window's `time_s` values stay on the capture's own timeline (a
window starting at 10 s reports 10.000, not 0.000), so its numbers can be
quoted against the whole capture without re-basing.

Three deliberate refusals to relay rather than work around:

- `capture_sha256` comes back `null` with `W_PARTIAL_INTEGRITY`: only the
  chunks touched were verified, so the whole-file digest is not claimed.
- A window whose start falls inside a gap is refused, not moved. The error
  names the sample to start at or before, and the one to resume at; a
  shifted window answers a different question.
- `--window` is not offered on `decode` or `assert`. A decoder carries sync
  state across block boundaries, so a windowed decode is not a slice of a
  full decode. On `measure`, `--window` and `--annotations` do not combine:
  annotations can point anywhere in the capture, so that path reads the
  file whole and the window has no effect. Pick one.

## Long recordings (minutes to hours)

Prefer several bounded segments over one unbounded capture — each is an
independently verifiable artifact:

```bash
for i in $(seq -w 1 12); do
  ppk2lab capture --device S --duration 5min \
    --output "run-$i.ppk2a" --json > "run-$i.result.json" || true
done
```

- Disk budget: the device sends 4-byte sample words at 100 kS/s, so the
  raw stream is 400 kB/s (~1.4 GB/hour pre-compression); check free space
  and report the projection first. A CSV export of that data is far
  larger — measured at 52.1 bytes per sample on a real capture, against
  130.9 bytes per bucket for the decimated form.
- Summarize each segment with `ppk2lab inspect run-01.ppk2a --json`, and
  `ppk2lab measure run-01.ppk2a --json` when the samples are needed.
- A whole-series overview without a multi-gigabyte CSV:
  `ppk2lab export run-01.ppk2a --format csv --output run-01-1s.csv --bucket-ms 1000`
  writes one record per bucket with mean, min, max, charge, per-range
  occupancy and switch count. It is a summary, not the series — its header
  shares no column name with the raw export, it carries `W_DECIMATED`, and
  each bucket says how many of its samples were present, missing, excluded
  and saturated. Min and max are what preserve peaks; a bucket mean alone
  does not. The per-bucket accounting is exact: over a 60 s capture at
  `--bucket-ms 100`, 11 of the 600 buckets came back `complete: 0` and
  their `missing_in_bucket` summed to 65,024 — the capture's whole
  measured loss. The worst bucket held 4,384 samples against 5,616
  missing, so it is the bucket flags, not the mean, that tell you which
  points of a plot are trustworthy.
- Before committing to a large write, read `estimated_bytes`,
  `bytes_per_record` and `size_basis` from the export result — they are
  measured on a prefix of this capture's own records, not a constant.
- USB unplug (`transport_error`): run doctor, reconnect, continue with the
  next segment; never delete or "repair" an interrupted artifact. A capture
  that fails on the final rename is preserved and its path is named in the
  remediation — it is a complete, readable artifact by then. Hot unplug has
  never been exercised on hardware — the cable was not pulled mid-capture
  in the one session run — so treat this path as designed, not verified.
- An interrupted capture keeps what it had. Measured: a `SIGTERM` mid-run
  preserved 357,888 samples in a readable artifact with the interruption
  recorded, exit 6, and no orphan temp file; after a `SIGKILL` the next
  open discarded 17,412 stale stream bytes and read metadata cleanly.
  Report the partial artifact, don't discard it — but read
  `interruption.reason` with the caveat in diagnostics.md.
- Rising gap counts across segments usually mean host load — recommend
  closing other software or a powered hub.
- Report a per-segment table (duration, mean/peak, gaps, complete); flag
  incomplete segments explicitly, never average them in silently.

Done when: every artifact is reported with its `complete` flag, its
measured loss, and its `capture_sha256` — and any DUT-powered measurement
was taken inside one open session, with a plausible non-zero current as the
evidence that power was actually on.
