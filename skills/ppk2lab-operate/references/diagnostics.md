# Hardware diagnostics

Read-only by default; a short verification capture is measurement-only.
Never change voltage or DUT power "to test a theory" without asking.

## `doctor` first — it is a gate, not a printout

`ppk2lab doctor --json` **exits nonzero when a check fails**, so it belongs
at the front of any script that is about to touch hardware:
`ppk2lab doctor --json || abort`. `warn` and `skip` stay non-blocking, and
`--simulate doctor` still exits 0. Each failing check carries its own
`remediation` and the `exit_code` it maps to, so the abort reason is
already written for you.

A warning is not a failure, and the first `doctor` run against a healthy
unit is not expected to be clean. On the one PPK2 measured so far it
reported 11 pass, 1 warn, 1 skip, exit 0 — the warning being
`calibrated_flag`, below.

Four checks decide how a diagnosis should be worded:

- `calibrated_flag` warns when the device reports `Calibrated: 0`. The
  measured unit reports exactly that **while carrying calibration constants
  for all five ranges**, and every conversion works; the `calibration`
  check passes on the same run. The flag's meaning is not hardware-verified
  and this contradiction is recorded, not explained. So: do not stop, do
  not call the device uncalibrated, and do not offer to recalibrate
  anything. Report it with the firmware fingerprint and say absolute
  accuracy is unconfirmed — which it is anyway, since no calibrated
  reference has ever been used (analysis.md).
- `device_selection` warns when several PPK2s are attached and no
  `--device`/`--port` was given. The evidence is still valid; it is just
  evidence about one unit, and the report has to name which.
- `metadata_read` fails (exit 5) when metadata cannot be read at all —
  without it there are no calibration constants and raw codes cannot become
  current.
- `firmware_fingerprint` (HW, IA, metadata key count, port count) is the
  only identity a PPK2 offers: the measurement port reports no version
  string. Quote it in every diagnosis and every compatibility report. It is
  also in `ppk2lab info --json` and in every capture manifest, so a stored
  artifact can be attributed to a unit after the fact.

## Symptom → first checks

| Symptom | First checks |
|---|---|
| All D0-D7 constant despite DUT activity | Logic VCC (pin 10) connected to DUT VCC (1.65-5.5 V)? Shared GND? |
| One channel dead, others fine | Pinout: pin 1 GND, pins 2-9 = D0-D7; walking-one from the DUT |
| Random digital noise | Floating unconnected inputs — expected, not a fault |
| Current ~0 in source mode | Almost always an unpowered DUT, and the reading is plausible enough to be believed by mistake. If power was enabled by a separate `configure --dut-power on --apply`, it was already gone: measured off in every trial 500 ms after the port closed. Re-measure inside one open session (capture.md). Power state has no readback, so the current is the only evidence either way |
| `doctor` warns `calibrated_flag` | Expected; the measured unit reports `Calibrated: 0` with all five ranges calibrated. Not a fault and not a reason to stop — see above |
| A few gaps per minute, all `host_overflow` | Normal: 0.43% over 60 s on an idle macOS host, 1.08% under CPU and disk load. Close other software or move off an unpowered hub if it matters; report the loss either way. Investigate the physical path only when the reason is `counter_skip`, `stream_desync` or `usb_stall` |
| `interruption.reason: keyboard_interrupt` | A `SIGTERM` from a process manager produces this string too. The wording is imprecise and known to be — don't report that an operator interrupted the run unless something independent says so |
| `session_recovery` warning at open | The *previous* session was killed mid-stream; this open discarded its leftovers (17,412 stale bytes after a `SIGKILL`, measured) and read metadata cleanly. Nothing to repair in this run |
| `W_CLIPPED` / `saturated_samples > 0` | The ADC sat on its full-scale code: the reading was pinned, not measured. In the top range the load exceeded the instrument's 1 A span — stop and inspect physically. In a lower range (`saturated_ranges` names it) the auto-range switch had not completed, which is a transient, not a DUT fault |
| Spikes at level changes | Range-switch transients; compare raw vs `--filtered` before blaming the DUT. The filter's settling window is an unmeasured assumption — no capture on hardware has yet crossed a range boundary — so report both series if the filtered one changes the answer |
| `rate_check: deficit` | The timeline advanced slower than the wall clock: samples were lost beyond what the counter reports. Same causes as above, but the loss is larger than the gap table suggests |
| `W_STREAM_DESYNC` / `implausible` samples | Byte-level framing was lost (a partial byte run vanished). Usually a marginal cable, hub, or a saturated USB controller |
| `W_UNACCOUNTED_SAMPLES` | Wall time accounts for more samples than the timeline does, beyond what host anchoring and clock tolerance explain. The loss is real and capture-level — no window can localize it, so do not look for it in the gap table |
| `energy_uj: null` | Not a fault: ampere mode has no defensible supply voltage. See analysis.md |

Look up any code you do not recognize in
`ppk2lab capabilities --json` (`warning_codes`, `gap_reasons`,
`interruption_reasons`); `gap_reasons` in particular says *where* a loss
happened — device or transit, host queue, framing, transport — which is
usually the fastest route to the physical cause.

## Workflow

1. Gate first: `ppk2lab doctor --json` (nonzero means stop and remediate).
2. Reproduce cheaply:
   `ppk2lab capture --device S --duration 1s --output diag.ppk2a --json`
3. Look at it before loading it: `ppk2lab inspect diag.ppk2a --json` —
   manifest only, so it works on an artifact of any length.
4. Digitals: `ppk2lab export diag.ppk2a --format vcd --output diag.vcd`;
   `x` regions are gaps.
5. Current: `ppk2lab measure diag.ppk2a --json` — check `invalid_range`,
   `not_convertible`, `saturated_samples`, gap counts, and physical
   plausibility of min/max against `p50`.
6. Report: observed symptom → evidence (sample ranges) → most likely
   physical cause → suggested fix, with the firmware fingerprint attached.
   Aim to narrow to at most two physical candidates, each with its
   supporting evidence window.

Done when: the symptom is attached to specific evidence, each candidate
cause names what would distinguish it, and anything that only *looks* like
a fault — a `calibrated_flag` warning, host-overflow gaps, negative
readings at very low current — has been named as expected behaviour rather
than escalated.
