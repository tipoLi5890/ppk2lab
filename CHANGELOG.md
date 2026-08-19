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

### Added (release preparation, 2026-08-19)

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
