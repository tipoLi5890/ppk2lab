# Observable PPK2 host protocol

This document records the PPK2 serial protocol as used by ppk2lab, based on
Nordic Semiconductor's official PPK2 documentation and the official Power
Profiler app repository as behavior references (see `docs/sources.md`). It
describes observable behavior only.

## Transport

- USB CDC ACM; Nordic VID `0x1915`, PPK2 PID `0xC00A`.
- Firmware 1.2.0+ exposes a second (shell) CDC interface. The measurement
  port is classified by the lowest USB interface number; when the OS does
  not expose interface numbers and more than one candidate exists, ppk2lab
  refuses to guess and asks for an explicit `--port`.
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
- detect byte-level framing loss (a lost run that is not a multiple of four
  shifts every later word, making each look like a fresh counter jump) by
  the running mismatch rate, and re-align by scoring the four candidate byte
  offsets against counter continuity;
- account for host-side losses (queue overflow) by byte count, including
  4-byte realignment when the dropped size is not a multiple of 4;
- build timestamps as `capture_start + timeline_index * 10 us`, where gaps
  advance the timeline — later samples are never shifted earlier;
- treat range values above 4 as invalid samples (kept, flagged), not errors.

## Hardware observations (2026-08-19, one PPK2 on macOS)

- macOS pyserial exposes no USB interface numbers (`location` is identical
  for both CDC ports), so port roles cannot be classified passively; the
  measurement port is identified by a read-only metadata probe (stop +
  drain + 0x19), which the driver now performs automatically.
- A device left measuring by an interrupted session keeps streaming (or
  leaves a large stale buffer in the OS driver); opening therefore always
  sends stop and drains input before the metadata query.
- Observed metadata on real hardware: `Calibrated: 0`, R0 ≈ 1000.625 Ω,
  values printed with 20 decimal places, `HW: 49625`, `IA: 59`, field order
  differs from older examples (VDD/HW/mode appear before S/I families) —
  the order-tolerant parser handles all of this.
- The calibration expression is dimensionally amperes (R constants are real
  shunt ohms); the API multiplies by 1e6 to report microamperes
  (docs/calibration.md).
- Sustained streaming at 100 kS/s: 1 s and 2 s captures completed with zero
  counter gaps.

## Unverified items (tracked for the hardware matrix)

- Behavior of every legacy opcode on firmware 1.2.4.
- Byte order of the float in the user-gain command `0x25` (little-endian
  assumed pending hardware confirmation).
- Measurement-interface identification fields on Windows and Linux (macOS
  requires the metadata probe; other platforms may expose interface
  numbers).
- Full metadata field variants across hardware revisions and firmware
  versions.
- Counter behavior across long USB stalls.
- Precision known-load calibration cross-check against the official app.
