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

Durations take `us`, `ms`, `s`, `min`, `h` — `--duration 8h` is a soak run,
not a typo. An output path that is a directory is refused before the first
sample is recorded, not after the whole capture.

Verify each configure change's `after` state; relay any
`observed_after: false` (DUT power has no readback). Report from the
capture result: `complete`, stored/missing samples, mean/peak current,
charge/energy, path, `capture_sha256`.

`complete: false` (exit 6): data is preserved with loss markers — report
the gap table and interruption reason; offer a retry with lower system
load or shorter duration. On session close the library restores the
starting power state; relay restoration warnings.

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
witnessed and shows up as `W_UNACCOUNTED_SAMPLES`. Anchoring jitter and
clock tolerance are stated assumptions, not measurements from this
project's hardware — say so if a conclusion rests on them.

Without `--output` the capture is held in RAM and is refused beyond 60 s;
pass `--output` (preferred) or `--in-memory` if buffering is genuinely
intended.

## Look before you load

`ppk2lab inspect run.ppk2a --json` reads the manifest and never touches a
sample chunk: identity, duration, gap count, calibration, firmware
fingerprint, interruption, and the statistics the capture recorded at write
time. Use it first on any artifact you did not just create, and on every
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
window, not the file — with a floor of about one 1 M-sample chunk, which is
CRC-checked whole before it can be sliced. Three deliberate refusals to
relay rather than work around:

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

- Disk budget: raw stream is ~400 kB/s (~1.4 GB/hour pre-compression);
  check free space and report the projection first.
- Summarize each segment with `ppk2lab inspect run-01.ppk2a --json`, and
  `ppk2lab measure run-01.ppk2a --json` when the samples are needed.
- A whole-series overview without a multi-gigabyte CSV:
  `ppk2lab export run-01.ppk2a --format csv --output run-01-1s.csv --bucket-ms 1000`
  writes one record per bucket with mean, min, max, charge, per-range
  occupancy and switch count. It is a summary, not the series — its header
  shares no column name with the raw export, it carries `W_DECIMATED`, and
  each bucket says how many of its samples were present, missing, excluded
  and saturated. Min and max are what preserve peaks; a bucket mean alone
  does not.
- Before committing to a large write, read `estimated_bytes`,
  `bytes_per_record` and `size_basis` from the export result — they are
  measured on a prefix of this capture's own records, not a constant.
- USB unplug (`transport_error`): run doctor, reconnect, continue with the
  next segment; never delete or "repair" an interrupted artifact. A capture
  that fails on the final rename is preserved and its path is named in the
  remediation — it is a complete, readable artifact by then.
- Rising gap counts across segments usually mean host load — recommend
  closing other software or a powered hub.
- Report a per-segment table (duration, mean/peak, gaps, complete); flag
  incomplete segments explicitly, never average them in silently.
