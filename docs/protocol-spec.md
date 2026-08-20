# Observable PPK2 host protocol

This document records the PPK2 serial protocol as used by ppk2lab, based on
Nordic Semiconductor's official PPK2 documentation and the official Power
Profiler app repository as behavior references (see `docs/sources.md`). It
describes observable behavior only.

## Transport

- USB CDC ACM; Nordic VID `0x1915`, PPK2 PID `0xC00A`.
- Firmware 1.2.0+ exposes a second (shell) CDC interface. The measurement
  port is classified by the lowest USB interface number; when the OS does not
  expose interface numbers and more than one candidate exists, discovery
  leaves every role `unknown` rather than guessing, and `PPK2.open()`
  identifies the measurement port by trying each candidate with the read-only
  metadata probe (stop + drain + `0x19`). Only when no candidate answers does
  it fail and ask for an explicit `--port`.
- Line coding 115200 8N1 by default (the official app's setting). Actual
  throughput (~400 kB/s of sample data) is USB-bound, not UART-bound; the
  baud rate is overridable for behavior testing.

## Commands (stable subset)

Only opcodes exercised by the current official app path are part of the
stable surface:

| Opcode | Payload | Purpose |
|---:|---|---|
| `0x06` | — | start continuous measurement |
| `0x07` | — | stop measurement |
| `0x0C` | `0x00`/`0x01` | DUT power off/on |
| `0x0D` | big-endian `uint16` millivolts | set source voltage (800-5000 mV) |
| `0x11` | `0x01` ampere / `0x02` source | set measurement mode |
| `0x19` | — | read metadata (text, terminated by `END`) |
| `0x20` | — | reset |
| `0x25` | range byte + 4-byte IEEE 754 float | set user gain for one range |

Legacy opcodes visible in older public material (trigger, range, switch
point, resistor, spike filtering) are intentionally not exposed: some are
firmware-unsupported and at least one external-trigger opcode is reported
inconsistently. They stay out of the stable API until a firmware-by-command
hardware test matrix proves them.

## Metadata reply (0x19)

Newline-delimited `KEY: VALUE` text ending with `END`. Observed keys:

`Calibrated`, `R0..R4`, `GS0..GS4`, `GI0..GI4`, `O0..O4`, `S0..S4`,
`I0..I4`, `UG0..UG4`, `VDD`, `HW`, `mode`, `IA`.

Parser requirements (implemented in `ppk2lab.protocol.metadata`):

- tolerate unknown keys (preserved in `extras`), reordering, CRLF/LF, `NaN`
  values, and truncated replies;
- never treat malformed content as fatal — report warnings and keep missing
  fields as `None`.

## Sample stream

While measuring, the device streams 4-byte little-endian `uint32` samples at
100 kS/s:

```text
bits  0-13   14-bit ADC field (×4 before calibration)
bits 14-16   current measurement range (valid 0-4)
bit  17      reserved
bits 18-23   6-bit rolling sample counter (0-63)
bits 24-31   digital inputs; bit N = D<N>
```

Host requirements:

- reassemble 4-byte frames across arbitrary chunk boundaries;
- detect loss via `missing = (actual - expected) & 0x3f`. Two consequences
  follow from the counter being 6 bits wide, and both are load-bearing:
  a loss of 64 samples or more aliases (100 lost samples are reported as
  36), and a loss of *exactly* k*64 samples leaves the counter continuous,
  producing no gap event at all. Counter-derived gaps are therefore always
  flagged ambiguous, and the wall-clock cross-check below is the only
  witness to the invisible class;
- cross-check the sample timeline against elapsed wall time between the
  first and last received samples. A capture that advanced far slower than
  100 kS/s lost samples however quiet the counter stayed; every capture
  records `achieved_sample_rate_hz` and `rate_deficit_ratio`;
- treat a counter mismatch as *unconfirmed* until the following samples
  prove the framing survived. A byte-level loss whose length is not a
  multiple of four shifts every later word, so its counter field reads bits
  belonging to its neighbours and each word looks like a fresh skip;
  committing those immediately would advance the timeline by fabricated
  amounts. A mismatch therefore commits only after several correctly
  incrementing samples, while four consecutive mismatches (or eight within
  sixteen samples) declare a desync instead — the verdict always arrives
  first, so no gap derived from a shifted counter is ever committed;
- on a desync, discard the shifted words rather than passing them off as
  measurements, and re-align by scoring the four candidate byte offsets
  against counter continuity, requiring a minimum score so unrecoverable
  bytes are dropped once instead of re-entered word by word;
- account for host-side losses (queue overflow) by byte count, including
  4-byte realignment when the dropped size is not a multiple of 4;
- build timestamps as `capture_start + timeline_index * 10 us`, where gaps
  advance the timeline — later samples are never shifted earlier;
- treat range values above 4 as invalid samples (kept, flagged), not errors;
- treat a sample whose 14-bit ADC field sits on its full-scale code
  (`0x3FFF`) as pinned rather than measured: the input was at or beyond the
  top of the selected range, so the calibrated value is a ceiling and no
  arithmetic on it recovers what the DUT drew. In the top range the pinned
  value lands at the top of the instrument's 1 A span — inside the >1.1 A
  implausibility limit — so an over-range load cannot be caught by any test
  on the converted current. It has to be caught at the ADC code.

## Hardware observations

Everything below was measured on **one** PPK2 on macOS, in sessions on
2026-08-19 and 2026-08-20. Every row of a compatibility claim is keyed on the
firmware fingerprint it was seen under; this one is
`HW=49625 IA=59.0 keys=40 ports=2`. One unit on one OS is not a matrix — see
"Unverified items" for what that leaves open.

### Enumeration and port roles

- macOS pyserial exposes no USB interface numbers (`location` is identical
  for both CDC ports), so both ports enumerate as `role: unknown` and roles
  cannot be classified passively. The measurement port was identified by the
  read-only metadata probe, which the driver performs automatically.
- VID `0x1915`, PID `0xC00A`, two CDC ports, as documented above.

### Metadata reply

- The reply carried **40 keys**, exactly the families listed under "Metadata
  reply" above (`Calibrated`, five each of R/GS/GI/O/S/I/UG, `VDD`, `HW`,
  `mode`, `IA`), with values printed to 20 decimal places and a field order
  that differs from older examples (VDD/HW/mode appear before the S/I
  families) — which the order-tolerant parser handles.
- Shunt resistances on this unit: R0 1000.6250, R1 101.4608, R2 10.2309,
  R3 0.9629, R4 0.0559 Ω. All five user gains read 1.0.
- **`Calibrated: 0`, while all five ranges carry complete constants.** The
  flag's meaning is not hardware-verified, so ppk2lab converts and warns
  (`doctor` reports `calibrated_flag` as a warning; `W_NOT_CALIBRATED`).
  Recorded as observed, not explained.
- The calibration expression is dimensionally amperes (R constants are real
  shunt ohms); the API multiplies by 1e6 to report microamperes
  (docs/calibration.md).

### Interrupted sessions

- A device left measuring by an interrupted session keeps streaming (or
  leaves a large stale buffer in the OS driver); opening therefore always
  sends stop and drains input before the metadata query.
- Measured: after a `SIGKILL` mid-capture, the next open discarded **17,412
  stale stream bytes** and then read metadata cleanly. `doctor` reported
  `session_recovery` as a warning carrying that exact count.

### Sustained streaming at 100 kS/s

Two 60 s captures, differing only in what else the host was doing. Both
predate the Unreleased fix for this project's own artifact writer, which was
stalling the consumer at every chunk boundary; the loss below is therefore
mostly not a property of the host or the instrument. Recorded here because it
is what the protocol layer saw:

| host state | missing samples | share | gaps | gap sizes (samples) |
|---|---|---|---|---|
| idle | 25,792 | 0.43% | 5 | 4864, 5008, 5136, 5136, 5648 |
| 12 CPU spinners + continuous disk writes | 65,024 | 1.08% | 5 | 9360, 10288, 10960, 11312, 23104 |

- **Every gap was `host_overflow`** — the host's bounded queue dropped whole
  chunks. Under load the gaps grew *larger* rather than more numerous. A
  third 60 s capture later in the same session lost 304,847 samples (5.1%),
  so the rate follows whatever else the machine is doing and no figure here
  is a specification.
- No `counter_skip`, `usb_stall` or `stream_desync` gap has appeared in any
  recorded hardware session. Those paths are exercised only by the mock
  transport, so their handling is tested but not witnessed.
- In all three captures `timeline.rate_check` stayed `ok`, with
  `unaccounted_samples_estimate` near −230. That is the correct answer, not a
  miss: the gap table already accounted for every lost sample, so the
  wall-clock witness had nothing to add. It exists for the loss class the
  6-bit counter cannot see, which this was not — so `rate_check: ok` is never
  a claim that a capture is gap-free.

### Sample clock against the host clock

Three captures, varying only length:

| capture | device time | wall measured | difference |
|---|---|---|---|
| 3 s | 3.000000 s | 2.997710 s | −2.290 ms |
| 60 s | 60.000000 s | 59.997747 s | −2.253 ms |
| 60 s, under load | 60.000000 s | 59.997400 s | −2.26 ms |

- The anchoring error is a **fixed ≈ −2.27 ms**, the same at 3 s and at 60 s,
  so it is an offset and not a proportional drift. With it removed the device
  clock agreed with this host to within ~10 ppm.
- Consequently `achieved_sample_rate_hz` read 100076 Hz over 3 s and
  100004 Hz over 60 s: on a short capture that field is dominated by the fixed
  offset, so a short capture is not a way to measure the sample rate.

### DUT power does not outlive the host connection

Varying only how long the serial port stayed closed between enabling DUT
power and reopening to check it:

| port closed for | powered on reopening |
|---|---|
| 0 ms | 3/3 |
| 100 ms | 1/3 |
| 250 ms | 2/3 |
| 500 ms | 0/3 |
| 1000 ms | 0/3 |
| 4000 ms | 0/3 |

The device de-energizes VOUT once the USB host goes away — off in every trial
from 500 ms on. This is a fail-safe in the instrument, not something the host
asked for. Mode and source voltage are metadata-backed and do persist; only
the output drops. `configure --dut-power on --apply` therefore warns
`W_DUT_POWER_TRANSIENT`, and a powered measurement has to happen inside one
open session.

## Unverified items (tracked for the hardware matrix)

- Behavior of every legacy opcode on firmware 1.2.4.
- Byte order of the float in the user-gain command `0x25` (little-endian
  assumed pending hardware confirmation; every gain observed so far read 1.0,
  so nothing has yet depended on it).
- Measurement-interface identification fields on Windows and Linux (macOS
  requires the metadata probe; other platforms may expose interface
  numbers). Nothing here has been run on Windows or Linux hardware.
- Metadata field variants across hardware revisions and firmware versions:
  one fingerprint is recorded, which is a single row rather than a matrix.
- Counter behavior across long USB stalls, and across a hot unplug — the
  cable has never been pulled mid-capture.
- Precision known-load calibration cross-check against a calibrated
  reference. A ±5% resistor check has been run and the reading is consistent
  with the load, which rules out a gross error and nothing more: the
  resistor's own tolerance is not small enough to resolve the instrument's
  gain error. That needs a resistor an order of magnitude tighter, or a
  calibrated reference (docs/SPEC.md, "Measurement uncertainty").
