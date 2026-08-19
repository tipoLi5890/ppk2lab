# Public API baseline (`0.2.0`)

## 1. Purpose

This document is the **frozen surface** for the `0.2.0` release gate: the
Python names, CLI commands, exit codes, and file formats that downstream code
and agents may depend on, and the checklist the release gate diffs against.

- **Additions** (exported names, result fields, schemas, optional CLI flags)
  are backward compatible and may land in a patch or minor release.
- **Removals, renames, and meaning changes** require a `SCHEMA_VERSION` bump
  (for machine-readable contracts) plus a CHANGELOG migration note; see
  [SPEC.md - Stability policy](SPEC.md#stability-policy).
- Anything not listed here is internal (section 5).

Current: `ppk2lab.__version__ = "0.2.0.dev0"`, `ppk2lab.SCHEMA_VERSION = "1"`.

## 2. Stable Python surface (`0.2.0`)

Every name in `ppk2lab.__all__` (24 exports):

| Name | Kind | Description |
|---|---|---|
| `PPK2` | class | Synchronous device handle; `PPK2.open(serial_number=, port=, transport=, simulate=, simulator=, read_metadata=)`, context manager, `refresh_metadata`, `set_mode`, `set_source_voltage_mv`, `set_dut_power`, `set_user_gain`, `reset`, `start_measuring`, `stop_measuring`, `stream`, `capture`, `recover_session`, `close`. `stream()` returns a closable `StreamIterator`; `with device.stream(...) as events:` is the documented idiom. |
| `AsyncPPK2` | class | Async mirror of `PPK2` (`await AsyncPPK2.open(...)`, async context manager, `async def` state-changing and capture methods, `async for` over `stream()`). `stream()` returns a closable `AsyncStreamIterator`; `async with adev.stream(...) as events:` is the documented idiom. Blocking calls run via `asyncio.to_thread`. |
| `discover` | function | `discover(*, simulate=False) -> list[DeviceInfo]`; read-only device enumeration. |
| `DeviceInfo` | class | Frozen dataclass: `serial_number`, `vid`, `pid`, `ports`, `firmware_version`, `simulated`, `measurement_port`, `to_json()`. |
| `DeviceState` | class | **Frozen** dataclass: `mode`, `source_voltage_mv`, `dut_power`, `measuring`, `source_voltage_basis`; `None` means unknown. Reached through the read-only `PPK2.state` property — the host cannot assert a hardware state the device never confirmed. |
| `StateChange` | class | Dataclass record of one state-changing operation: `operation`, `requested`, `before`, `after`, `applied`, `observed_after`, `warnings`. |
| `Mode` | class | `IntEnum` with `AMPERE = 1`, `SOURCE = 2` (wire values). |
| `GapEvent` | class | Frozen dataclass for sample loss: `index`, `missing`, `reason`, `ambiguous`. |
| `Capture` | class | In-memory capture model over raw samples: `load`/`save`, `stored_count`, `start_index`, `end_index`, `missing_known`, `duration_s`, `sample_rate_hz`, `calibration`, `source_voltage_mv`, `raw_bytes`, `sha256`, `stored_to_timeline`, `timeline_to_stored`, `time_to_index`, `index_to_time`, `iter_events`, `logic_bytes`, `currents_ua`, `summary`. |
| `CaptureResult` | class | Dataclass returned by `PPK2.capture()`: `capture`, `path`, `stats`, `complete`, `interruption`, `trigger`, `capture_id`, `capture_sha256`, `warnings`, `to_json()`. |
| `read_capture` | function | `read_capture(path) -> Capture`; reads a `.ppk2a` artifact. |
| `write_capture` | function | `write_capture(capture, path, *, overwrite=False) -> str`; writes a `.ppk2a` artifact. |
| `Calibration` | class | Per-range calibrated ADC conversion: `Calibration(ranges, *, vdd_mv=, calibrated=)`, `from_metadata`, `missing_ranges`, `convert`, `convert_block`. |
| `Annotation` | class | One decoded event tied to an exact sample range: `decoder`, `kind`, `start_sample`, `end_sample`, `fields`, `confidence`, `errors`, `to_json`/`from_json`. |
| `UARTDecoder` | class | `UARTDecoder(rx, baud, *, data_bits=8, parity=None, stop_bits=1, msb_first=False, invert=False, sample_rate=SAMPLE_RATE_HZ, allow_experimental=False)`. |
| `SPIDecoder` | class | `SPIDecoder(sclk="D1", mosi=None, miso=None, cs=None, *, mode=0, word_bits=8, msb_first=True, cs_active_low=True, expected_clock_hz=None, idle_timeout_samples=None, sample_rate=SAMPLE_RATE_HZ, allow_experimental=False)`. |
| `decode_capture` | function | `decode_capture(capture, decoder) -> list[Annotation]`; offline, gap-aware decode of a whole capture. |
| `Ppk2labError` | class | Base exception for every ppk2lab error; carries stable `code`, `exit_code`, `remediation`, and `to_json()`. All library errors subclass it. |
| `SAMPLE_RATE_HZ` | constant | `100000` — the fixed shared current + D0-D7 sample rate. |
| `DIGITAL_CHANNELS` | constant | `("D0", ..., "D7")` in bit order. |
| `VOLTAGE_MIN_MV` | constant | `800` — minimum source voltage. |
| `VOLTAGE_MAX_MV` | constant | `5000` — maximum source voltage. |
| `SCHEMA_VERSION` | constant | `"1"` — version of the machine-readable contracts, independent of the package version. |
| `__version__` | constant | Package version string. |

Also stable at the subpackage level (imported by path, not re-exported at top
level): `ppk2lab.schemas` (`SCHEMAS`, `list_schemas`, `get_schema`) and the
`ppk2lab.analysis` entry points named in [SPEC.md](SPEC.md#python-api-surface-sync).

## 3. Stable CLI surface

Executable: `ppk2lab`. Twelve subcommands, frozen for `0.2.0`: `discover`,
`info`, `capabilities`, `schema`, `doctor`, `configure`, `capture`, `inspect`,
`decode`, `measure`, `assert`, `export`. All are read-only except `configure`
(dry-run unless `--apply`) and `capture` (starts/stops measuring only; never
enables DUT power). `inspect` reads a capture's manifest without touching a
sample chunk, so it is bounded regardless of how long the recording is.

Global flags, accepted before or after the subcommand: `--json` (emit the JSON
envelope instead of human text), `--simulate` (simulated PPK2 — toolchain
testing, not a measurement), `--version`.

Envelope contract (schema `envelope`), identical for every command:

```json
{"schema_version": "1", "command": "capture", "ok": true,
 "result": {}, "warnings": [], "error": null}
```

`error` (schema `error`) always carries `code`, `message`, `remediation`, and
`exit_code`. Exit codes 0-9 are frozen; the authoritative table is
[SPEC.md - Exit codes](SPEC.md#exit-codes-frozen): 0 success, 1 assertion
failed, 2 usage/schema/invalid input, 3 device not found, 4 permission denied
or port busy, 5 protocol/firmware/metadata mismatch, 6 capture incomplete,
7 optional capability missing, 8 unsafe state change refused, 9 internal error.
Error code strings are generated from `ppk2lab.errors.ERROR_CLASSES` and
published by `ppk2lab capabilities --json`.

## 4. Stable file formats

- **Capture artifact `.ppk2a`** — zip container with `manifest.json` (schema
  `capture-manifest`, `format: "ppk2lab-capture"`, `format_version: 1`),
  chunked raw `u32le-v1` sample files, and optional `metadata.txt`. The format
  is **append-only**: newer readers open `format_version` 1 files; readers
  refuse a higher `format_version` explicitly rather than guessing. Raw samples
  plus the manifest are the source of truth.
- **Annotation JSONL** — one `annotation`-schema object per line (`decoder`,
  `kind`, `start_sample`, `end_sample`, `fields`, `confidence`, `errors`),
  written by `ppk2lab decode --output` and read back by `measure`/`assert`.
- **Derived exports** — `csv`, `vcd`, `jsonl` via `ppk2lab export --format`;
  reproducible views over a capture, never a replacement for it.
- **Decimated exports** — `ppk2lab export --decimate N` / `--bucket-ms M`
  emit one record per timeline bucket instead of one per sample. Opt-in only,
  never a default, and the header shares no column name with the raw export so
  the two can never be confused ([decimation.md](decimation.md)).
- **Schemas** (21, from `ppk2lab.schemas.SCHEMAS`, served by `ppk2lab schema`):
  `annotation`, `assert-result`, `capabilities-result`, `capture-manifest`,
  `capture-result`, `configure-result`, `decode-result`, `device`,
  `diagnostic`, `discover-result`, `doctor-result`, `envelope`, `error`,
  `export-result`, `gap`, `info-result`, `inspect-result`, `measure-result`,
  `state-change`, `timeline-check`, `window-stats`.

## 4b. Also stable at the subpackage level

Not every stable name is re-exported at the top level. These are part of the
`0.2.0` surface and follow the same change policy:

| Name | Kind | Description |
|---|---|---|
| `ppk2lab.types.VoltageBasis` | class | Enum recording how a voltage was learned (`caller_override`, `configured_source`, `device_metadata`, `unknown`). Energy is only derived from a defensible basis. |
| `ppk2lab.diagnostics` | module | `Diagnostic(code, message)`, `warn()`, `as_json()`, `warning_catalog()`, and the frozen `W_*` codes. |
| `ppk2lab.capture.stats.VoltageContext` | class | The voltage used for energy plus whether it is defensible. |
| `ppk2lab.capture.runner.timeline_report` | function | Wall-clock cross-check of the sample timeline. |
| `ppk2lab.errors.StreamStalledError` | class | A device that stopped streaming while keeping its port open (`STREAM_STALLED`). |
| `ppk2lab.errors.CaptureTooLargeError` | class | A capture too large to load whole (`CAPTURE_TOO_LARGE`, exit 2). Subclasses `UsageError`, not `CaptureFileError`: a soak artifact is not an invalid file. |
| `ppk2lab.errors.CalibrationUnavailableError` | class | A capture that carries no usable calibration, so no current can be derived from it. |
| `PPK2.firmware_fingerprint()` | method | Observable firmware identity (HW, IA, metadata key set, port count). `port_count` is `len(info.ports)`, so it is `0` for a device opened by path, which discovery never enumerated; the CLI turns that `0` into `null` in `info --json` and `doctor`, because the compatibility matrix is keyed on this dict and unknown must not read as zero. |
| `ppk2lab.device.StreamIterator` | class | Closable live-stream iterator: `__iter__`, `__next__`, `close()`, context manager. Released under a weakref identity check so a spent iterator cannot release a later stream's claim. |
| `ppk2lab.aio.AsyncStreamIterator` | class | Async mirror: `__aiter__`, `__anext__`, `aclose()`, async context manager. |
| `ppk2lab.capture.read_window` | function | `read_window(path, *, start_index=, end_index=, start_s=, end_s=, max_samples=) -> Capture`; reads only the chunks a window falls in. `Capture.load_window` is the classmethod mirror. |
| `ppk2lab.capture.stats.format_with_uncertainty` | function | Renders a value to the digits its uncertainty supports. Human output only; JSON keeps full precision. |
| `ppk2lab.capture.stats.StatePart` / `StateSplit` / `TypicalUncertainty` | class | Frozen dataclasses behind the `state_split` and `uncertainty` result blocks. |
| `ppk2lab.exports.decimate` | module | `Bucket`, `iter_buckets`, `export_decimated_csv`, `export_decimated_jsonl`, `bucket_samples_for_ms`, `DECIMATED_CSV_HEADER`. |
| `ppk2lab.exports.estimate_export_size` | function | Bytes an export would produce, measured on a prefix of this capture's own records. |
| `ppk2lab.capture.stats.compute_stats` | function | `compute_stats(capture, *, start_index=, end_index=, filtered=, assume_voltage_mv=, state_threshold_ua=) -> WindowStats`; the statistics the CLI and the assertion evaluator both use. |
| `ppk2lab.capture.stats.latency_until_below` | function | First timeline index where current stays below a threshold for N consecutive stored samples. A gap resets the hold — missing data never counts as evidence. |
| `ppk2lab.decoders.uart.uart_runs` / `uart_bytes` | function | The clean decoded byte stream, split at every discontinuity (`uart_runs`) or concatenated (`uart_bytes`). Unsynchronized and errored frames are excluded from both. |
| `ppk2lab.logic.transitions.edges` / `pulses` | function | Gap-aware digital edge and pulse extraction over `Capture.iter_events()`. |

Result fields added under the append-only policy: `voltage_basis`,
`voltage_measured`, `energy_note`, `charge_is_lower_bound`,
`samples.implausible`, `samples.covered_fraction`, `samples.unpopulated`,
`samples.saturated`, `samples.zero_code`, `current_ua.p50`/`p90`/`p99`/`p999`,
`distribution`, `state_split`, `uncertainty`, `saturated_samples`,
`saturated_ranges`, `samples_per_range`, `charge_per_range_uc`,
`range_switches`, `range_switch_rate_hz`, `unaccounted_samples_estimate`,
`unaccounted_loss_is_capture_level`, and `gaps_truncated` in window statistics;
`timeline` in capture results, with `first_sample_utc`, `anchor_uncertainty_s`,
`rate_offset_ratio`, `unaccounted_samples_estimate` and
`unaccounted_floor_samples`; `calibration`, `gaps_truncated`, and the
wall-clock fields in the capture manifest; `source_voltage_basis` in device
state; `warning_codes`, `gap_reasons`, `interruption_reasons` and per-option
`choices` in the capabilities manifest; `window`, `decimation`,
`estimated_bytes`, `bytes_per_record` and `size_basis` in export results, where
`capture_sha256` is now `string|null` — a windowed read verifies only the
chunks it touched, so the whole-file digest is reported as unverified rather
than as verified. Assertion observations carry `covered_fraction`,
`capture_end_sample`, and a machine-branchable `reason_code`. Envelope
`warnings` entries are `{code, message}` objects, and the published catalogs
carry `{code, category, meaning}`.

New assertion metrics: `p50_current` (alias `median_current`), `p90_current`,
`p99_current`, `p999_current`. A CI threshold on `max_current` drifts upward
with capture length because range switches accumulate; `p99_current` describes
a fraction of the distribution instead and does not.

## 5. Explicitly NOT frozen (may change without notice)

- **Internal modules and any name not in `ppk2lab.__all__`** — including
  `ppk2lab.transport`, `ppk2lab.protocol`, `ppk2lab.session`, `ppk2lab.cli`,
  `ppk2lab.triggers`, `ppk2lab.units`, and private helpers in otherwise public
  modules. `ppk2lab.logic` and `ppk2lab.exports` are internal **except** for
  the names listed in section 4b, which the documentation teaches and which
  are therefore frozen with everything else here.
- **`SpikeFilter` heuristic parameters** (`ppk2lab.calibration.SpikeFilter`,
  `settle_samples`, default `3`) — an experimental range-switch smoothing
  heuristic. The raw series is always preserved alongside it; do not build
  regression thresholds on filtered values.
- **The `--simulate` demo profile waveform** (`DemoActivityProfile` and the
  simulator's current/logic shape) — it exists to exercise the toolchain, not
  to be a stable measurement fixture.
- **`ppk2lab.testing` signal-builder defaults** — `uart_wave`, `spi_wave`,
  `uart_frame_bits`, `merge_logic`, and the profile classes are public so users
  can generate captures, but their default arguments and produced sample values
  may change. Pin arguments explicitly in tests.
- **Human-readable (non-`--json`) CLI text** — wording, column layout, and
  ordering. Parse `--json` output; never scrape the text output.

## 6. Change policy

`SCHEMA_VERSION` (`"1"`) governs the machine-readable contracts and is
independent of the package version. Adding fields is backward compatible;
removing or renaming fields, error codes, or exit codes requires a
`SCHEMA_VERSION` bump plus a CHANGELOG migration note, and the capture
`format_version` is append-only. The normative rules live in
[SPEC.md - Stability policy](SPEC.md#stability-policy); this document only
enumerates the surface those rules apply to.
