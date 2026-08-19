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
- `"deficit"` — it did not, so samples were lost beyond what the counter
  reports; `achieved_sample_rate_hz` and `rate_deficit_ratio` quantify it.
  Usual causes: a loaded host, an unpowered USB hub, a busy USB controller;
- `"too_short"` / `"not_applicable"` — the check could not run (a capture
  under two seconds, or a triggered capture whose pre-trigger buffer makes
  the comparison meaningless).

Without `--output` the capture is held in RAM and is refused beyond 60 s;
pass `--output` (preferred) or `--in-memory` if buffering is genuinely
intended.

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
- Summarize each segment with `ppk2lab measure run-01.ppk2a --json`.
- USB unplug (`transport_error`): run doctor, reconnect, continue with the
  next segment; never delete or "repair" an interrupted artifact.
- Rising gap counts across segments usually mean host load — recommend
  closing other software or a powered hub.
- Report a per-segment table (duration, mean/peak, gaps, complete); flag
  incomplete segments explicitly, never average them in silently.
