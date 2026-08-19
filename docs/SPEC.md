# Public data model, states, API, and schema contracts

Status: pre-`0.1.0`; everything here may change until the release freeze.
`SCHEMA_VERSION` is `"1"` and is independent of the package version.

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
  `stream_desync` (byte framing lost and re-aligned).

### Device state

`DeviceState` fields are `None` when unknown; unknown is preserved, never
defaulted: `mode` (`ampere`/`source`), `source_voltage_mv`, `dut_power`,
`measuring`, plus `source_voltage_basis` recording how the voltage was
learned (`configured_source` beats `device_metadata`, which is only a
regulator setpoint).

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
dev.stream(duration_s=..., sample_limit=...)      # yields SampleBlock | GapEvent
result = dev.capture(duration_s=5.0, output="run.ppk2a")  # -> CaptureResult
dev.close()                              # restores session-start power state
```

`ppk2lab.AsyncPPK2` mirrors the same surface with `async` methods and an
async `stream()` iterator.

Offline:

```python
cap = ppk2lab.Capture.load("run.ppk2a")
anns = ppk2lab.decode_capture(cap, ppk2lab.UARTDecoder(rx="D0", baud=9600))
from ppk2lab.analysis import measure_window, measure_annotations, parse_rule, evaluate_assertion
```

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

`warnings` is an array of `{code, message}` for the same reason: a program
deciding whether to retry cannot parse prose. Codes live in
`ppk2lab.diagnostics` (`W_SAMPLE_GAPS`, `W_TIMELINE_COMPRESSION`,
`W_VOLTAGE_ASSUMED`, `W_NOT_CALIBRATED`, …); new codes may be added, and an
existing code never changes meaning within a schema version.

### Voltage and energy

Results that carry energy also carry `voltage_measured: false` and a
`voltage_basis`, because the PPK2 measures current only — every energy
figure is charge times an assumption. In Ampere Meter mode the assumption is
not defensible and `energy_uj` is `null` unless the caller supplies the DUT's
real supply voltage. See docs/energy-analysis.md.

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

## Schemas

`ppk2lab schema --list` enumerates all published schemas; `ppk2lab schema
<name>` prints one. They are generated from the same definitions the code
uses (`ppk2lab.schemas`), and tests validate live CLI output against them.

## Stability policy

- Adding fields to results is backward compatible; removing or renaming
  fields, error codes, or exit codes requires a `SCHEMA_VERSION` bump and a
  CHANGELOG migration note.
- The capture `format_version` (currently 1) is append-only: newer readers
  open older files; older readers refuse newer files explicitly.
- Derived exports (CSV/VCD/JSONL) are reproducible views over raw captures
  and never replace them.
