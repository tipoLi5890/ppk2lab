# Public API baseline (`0.1.0`)

## 1. Purpose

This document is the **frozen surface** for the `0.1.0` release gate: the
Python names, CLI commands, exit codes, and file formats that downstream code
and agents may depend on, and the checklist the release gate diffs against.

- **Additions** (exported names, result fields, schemas, optional CLI flags)
  are backward compatible and may land in a patch or minor release.
- **Removals, renames, and meaning changes** require a `SCHEMA_VERSION` bump
  (for machine-readable contracts) plus a CHANGELOG migration note; see
  [SPEC.md - Stability policy](SPEC.md#stability-policy).
- Anything not listed here is internal (section 5).

Current: `ppk2lab.__version__ = "0.1.0.dev0"`, `ppk2lab.SCHEMA_VERSION = "1"`.

## 2. Stable Python surface (`0.1.0`)

Every name in `ppk2lab.__all__` (24 exports):

| Name | Kind | Description |
|---|---|---|
| `PPK2` | class | Synchronous device handle; `PPK2.open(serial_number=, port=, transport=, simulate=, simulator=, read_metadata=)`, context manager, `refresh_metadata`, `set_mode`, `set_source_voltage_mv`, `set_dut_power`, `set_user_gain`, `reset`, `start_measuring`, `stop_measuring`, `stream`, `capture`, `recover_session`, `close`. |
| `AsyncPPK2` | class | Async mirror of `PPK2` (`await AsyncPPK2.open(...)`, async context manager, `async def` state-changing and capture methods, `async for` over `stream()`). Blocking calls run via `asyncio.to_thread`. |
| `discover` | function | `discover(*, simulate=False) -> list[DeviceInfo]`; read-only device enumeration. |
| `DeviceInfo` | class | Frozen dataclass: `serial_number`, `vid`, `pid`, `ports`, `firmware_version`, `simulated`, `measurement_port`, `to_json()`. |
| `DeviceState` | class | Dataclass: `mode`, `source_voltage_mv`, `dut_power`, `measuring`; `None` means unknown. |
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

Executable: `ppk2lab`. Eleven subcommands, frozen for `0.1.0`: `discover`,
`info`, `capabilities`, `schema`, `doctor`, `configure`, `capture`, `decode`,
`measure`, `assert`, `export`. All are read-only except `configure` (dry-run
unless `--apply`) and `capture` (starts/stops measuring only; never enables
DUT power).

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
- **Schemas** (18, from `ppk2lab.schemas.SCHEMAS`, served by `ppk2lab schema`):
  `annotation`, `assert-result`, `capabilities-result`, `capture-manifest`,
  `capture-result`, `configure-result`, `decode-result`, `device`,
  `discover-result`, `doctor-result`, `envelope`, `error`, `export-result`,
  `gap`, `info-result`, `measure-result`, `state-change`, `window-stats`.

## 5. Explicitly NOT frozen (may change without notice)

- **Internal modules and any name not in `ppk2lab.__all__`** — including
  `ppk2lab.transport`, `ppk2lab.protocol`, `ppk2lab.session`, `ppk2lab.cli`,
  `ppk2lab.triggers`, `ppk2lab.logic`, `ppk2lab.exports`, `ppk2lab.units`, and
  private helpers in otherwise public modules.
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
