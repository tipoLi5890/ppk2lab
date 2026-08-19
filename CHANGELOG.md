# Changelog

All notable changes to this project are documented in this file. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project uses semantic versioning once released.

## [Unreleased] — 0.1.0.dev0

### Added

- PPK2 device driver: discovery with measurement/shell port classification,
  metadata parsing, Ampere/Source mode, source voltage (`voltage_mv`), DUT
  power, reset, user gain — every state change reported with
  requested/before/after and readback status.
- Loss-aware 100 kS/s stream parser: 4-byte framing across arbitrary chunk
  boundaries, 6-bit counter gap detection (modulo distance), host-drop
  accounting with byte-level realignment, truthful timeline (gaps advance
  time; data is never shifted).
- Calibration: documented conversion formula, per-range constants, NaN
  preservation for unknown calibration, optional range-switch spike filter
  (raw series always kept).
- Canonical capture artifact `.ppk2a` (versioned manifest + raw chunked
  `uint32` samples + gap table + CRC32/SHA-256, atomic writes, explicit
  overwrite).
- D0-D7 logic analysis: transitions, edges, pulses (gap-aware), VCD export
  with `x` states during gaps.
- Streaming decoders: UART (5-9 data bits, parity, stop bits, invert, bit
  order, break, framing/parity errors, fractional phase center sampling)
  and SPI (modes 0-3, 4-32 bit words, MSB/LSB, CS polarity or CS-less idle
  grouping). Rate feasibility tiers enforced (validated / conditional /
  experimental / unsupported); no decoding claimed across sample gaps.
- Software triggers: current threshold, digital edge/pattern, UART content,
  SPI content, with pre/post-trigger ring-buffer windows.
- Energy analysis: window and per-annotation charge/energy/peak/latency;
  assertion DSL + JSON rules with evidence windows, capture SHA-256, and
  JUnit report output.
- CLI `ppk2lab` with `discover`, `info`, `capabilities`, `schema`, `doctor`,
  `configure` (dry-run by default), `capture`, `decode`, `measure`,
  `assert`, `export`; stable JSON envelopes (`schema_version` 1), stable
  error codes with remediation, documented exit codes 0-9, and `--simulate`.
- Synchronous (`ppk2lab.PPK2`) and asynchronous (`ppk2lab.AsyncPPK2`) APIs.
- Mock transport and PPK2 simulator, plus public signal builders
  (`ppk2lab.testing`) used to self-generate every test vector.
- Two progressive-disclosure agent skills (`ppk2lab-operate`,
  `ppk2lab-maintain`): thin SKILL.md routers with the safety contract
  inline, per-task reference notes loaded on demand, and live knowledge
  queried from the self-describing CLI. Plus the Claude Code plugin
  manifest.

### Added (measurement-integrity hardening, 2026-08-19)

Properties that follow from the PPK2's own design — a 6-bit sample counter,
fixed 4-byte frames with no sync word, and an instrument that measures
current but never the DUT's voltage — are now enforced and tested
(`tests/test_measurement_integrity.py`) rather than left implicit.

- **Wall-clock timeline cross-check.** Every capture records
  `started_utc`/`ended_utc`, `wall_elapsed_s`, `achieved_sample_rate_hz`,
  and `rate_deficit_ratio`, and is marked incomplete
  (`interruption.reason = "timeline_compression"`) when the timeline
  advanced far slower than 100 kS/s. The 6-bit counter expresses at most 63
  missing samples, and a loss of exactly k*64 leaves it perfectly
  continuous, so elapsed wall time is the only possible witness.
- **Voltage provenance for energy.** Results carry `voltage_basis` and
  `voltage_measured: false`; Ampere-mode energy is `null` unless the caller
  passes the DUT's real supply voltage (`--assume-voltage-mv` /
  `assume_voltage_mv=`). Source-mode energy is computed from the setpoint
  and says so.
- **Calibration provenance in every capture**: `Calibrated` flag, missing
  ranges, metadata warnings, and non-unity user gains reach the result, the
  manifest's new `calibration` block, and `doctor`.
- **Coded warnings.** Envelope and capture `warnings` entries are now
  `{code, message}` (`ppk2lab.diagnostics`), so an agent can branch on
  `W_SAMPLE_GAPS`, `W_TIMELINE_COMPRESSION`, `W_VOLTAGE_ASSUMED`, and the
  rest instead of parsing prose.
- **Stall detection**: an idle-bytes budget plus an always-armed wall budget
  end a capture whose device stopped streaming without closing its port
  (`STREAM_STALLED`, `interruption.reason = "stream_stalled"`).
- **Framing desync detection and re-alignment**: a lost byte run that is not
  a multiple of four is detected by the running counter-mismatch rate,
  reported once as `stream_desync`, and re-aligned by scoring the four
  candidate byte offsets. Implausible currents (>1.1 A) are counted and
  excluded rather than averaged in.
- **Guards**: in-memory captures refused beyond 60 s without `--in-memory`;
  an optional source-voltage ceiling (`--max-voltage-mv`,
  `PPK2LAB_MAX_VOLTAGE_MV`) enforced before encoding; concurrent streams and
  mid-stream state changes refused instead of silently corrupting framing.
- **Statistics**: `covered_fraction`, `charge_is_lower_bound`, and
  `samples.implausible` so a gappy window cannot pass for a complete one.
- **Firmware fingerprint** (HW, IA, metadata key set, port count) recorded
  with every capture, since the measurement port reports no version string.
- `docs/faq.md` answering the ecosystem's recurring questions, including the
  ones whose honest answer is "the hardware cannot do that".

### Fixed

- Statistics dropped an unknown-size gap that sat exactly on a window's
  start boundary, letting the window claim it was complete.
- `run_capture` never armed the stream stall timeout, so a silent device
  could block a capture indefinitely.
- Windows USB locations (`1-4:x.0`) failed interface-number parsing, leaving
  every Windows port unclassified.
- Capture readers defaulted a missing `timeline.sample_rate_hz` instead of
  refusing the file, even though every timestamp derives from it.
- The serial hot path reassigned pyserial's timeout on every read.
- `PermissionDenied`/`PortBusy` remediation now names the platform's actual
  fix (another app on Windows/macOS, group membership on Linux).

### Changed

- The simulator defaults to Source Meter mode, matching what real hardware
  reported during validation (`mode: 2`).

### Added (release preparation, 2026-08-19)

- Development preview `0.1.0.dev0` published to PyPI via the trusted-publishing
  workflow (claims the package name; excluded from default pip resolution).

- `docs/api-baseline.md`: the frozen `0.1.0` public surface (24 Python
  exports, 11 CLI commands, exit codes, schemas, file formats) with the
  change policy.
- `docs/releasing.md`: the maintainer release procedure; external writes
  (tag, push, PyPI) remain gated on explicit maintainer authorization.
- `THIRD_PARTY_NOTICES.md`: dependency license audit — every declared
  runtime/optional/dev dependency verified against the MIT/BSD/Apache-2.0
  allowlist.
- Four-language README set (English canonical + `.github/` 繁體中文, 简体中文,
  日本語), GitHub PR template, and issue forms (bug, feature, compatibility
  report).

### Changed

- README restructured for a general audience (plain-language opening,
  Highlights section, simplified license wording); README and ROADMAP now
  track only remaining work — completed-milestone history lives here in the
  changelog.
- Agent skills consolidated from twelve prompts into two
  progressive-disclosure skills (`ppk2lab-operate`, `ppk2lab-maintain`).

### Fixed (first hardware validation pass, 2026-08-19)

- Calibration output unit: the documented formula is dimensionally amperes
  (R constants are real shunt ohms, confirmed on hardware); conversion now
  scales to microamperes correctly. Simulator constants regenerated to
  match.
- `PPK2.open()` now performs interrupted-session recovery (stop command +
  input drain) before reading metadata; a device left streaming by a
  previous session no longer corrupts the open sequence.
- When USB interface numbers are unavailable (observed on macOS), the
  measurement port is identified automatically by a read-only metadata
  probe instead of refusing to open.

### Security / safety

- DUT power is never enabled automatically by installation, discovery,
  diagnostics, or capture. `configure` requires `--apply`. Voltages are
  explicit millivolts and validated before any byte reaches the wire.
  Sessions restore the starting power state on close (fail-safe OFF with a
  recorded warning when the starting state was unknown).
