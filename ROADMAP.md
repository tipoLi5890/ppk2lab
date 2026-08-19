# Roadmap

The project ships its first public release as `0.1.0`, only after every
release gate below passes. Version numbers before that are `0.1.0.devN`. The
project is never labeled `1.0.0` as part of this plan.

## Current status

The full planned `0.1.0` feature set is implemented, with hardware-free
tests, lint, and types green. A first hardware validation pass succeeded on
one PPK2 on macOS, confirming device metadata, automatic port probing, and a
gap-free 10 s / 1M-sample capture.

## Measurement-integrity guarantees

Some of what this hardware cannot do is only visible once you work from the
sample format outward. These properties are consequences of the PPK2's own
design, and the implementation is built around them rather than around the
happy path. They are covered by `tests/test_measurement_integrity.py`.

- **The device cannot report large losses.** The sample counter is 6 bits
  (bits 18-23), so it expresses at most 63 missing samples; a larger loss
  aliases modulo 64, and a loss of exactly k*64 leaves the counter
  continuous — indistinguishable from no loss at all. Counter-derived gap
  sizes are therefore always marked ambiguous, and every capture is
  cross-checked against the host's wall clock, which is the only independent
  witness to the invisible class.
- **The instrument never measures the DUT's voltage.** It reports an ADC
  code and a range. In Source Meter mode the DUT runs from VOUT, so the
  configured setpoint is a defensible supply voltage; in Ampere Meter mode
  the DUT runs from its own supply, which never passes through the meter, so
  energy is `null` unless the caller supplies the real voltage. Results
  always carry `voltage_basis` and `voltage_measured: false`.
- **Unknown calibration must stay unknown.** A missing constant is never
  defaulted — a zero offset alone would bias every sample in that range by
  the full offset — and the reason travels with the capture in its
  `calibration` block.
- **Silence is not an error.** A USB CDC port that stops delivering data
  raises nothing, so the host imposes its own idle and wall budgets rather
  than waiting forever.
- **Frames have no sync word.** Losing a byte run that is not a multiple of
  four shifts every later 4-byte frame; that is detected from the counter
  mismatch rate, reported once, and re-aligned by scoring the four candidate
  offsets.

## Remaining work before 0.1.0

- OS/firmware compatibility matrix: Windows, macOS (Intel/AS), and Linux,
  against firmware 1.1.0, 1.2.0, and 1.2.4.
- Known-load calibration cross-check against the official Power Profiler app.
- UART/SPI decoder validation on real signals from an MCU fixture, against
  defined error-rate thresholds using golden captures.
- Multi-device session support validation.
- Hot-unplug recovery and 8-24 h soak tests.
- Codex plugin manifest verification against the current Codex release.
- Docs/examples reproducibility check from a clean environment.
- Dependency license re-scan at tag time (initial audit recorded in
  `THIRD_PARTY_NOTICES.md`).
- Release rehearsal per `docs/releasing.md`.
- PyPI publish (requires explicit maintainer authorization).

## `0.1.0` release gates

1. API, schemas, and capture format labeled with stability and version policy.
2. README/INSTALL/CLI examples reproducible from a clean environment.
3. At least one physical test pass on Windows, macOS, and Linux.
4. UART 9600 and SPI 10 kHz error-rate tests meet pre-defined thresholds on
   the hardware fixture.
5. No source code copied from other projects; no unlicensed firmware
   binaries in the repository.
6. All dependencies pass the license allowlist scan.
7. Claude Code and Codex skill/plugin installation and representative
   prompts verified.
8. CI and the hardware release workflow are green on the tag commit before
   any release or PyPI publish.

## Decoder support tiers (frozen for 0.1.0)

| Protocol / rate | Tier | Behavior |
|---|---|---|
| UART 1200-9600 baud | validated | full support, confidence 1.0 |
| UART 19200 baud | conditional | decodes with warning, confidence 0.7 |
| UART 38400 baud | experimental | requires `--allow-experimental`, confidence 0.4 |
| UART ≥ 57600 baud | unsupported | refused (`DECODER_RATE_UNSUPPORTED`) |
| SPI ≤ 10 kHz | validated | full support, confidence 1.0 |
| SPI 10-20 kHz | conditional | decodes with warning, reduced confidence |
| SPI 20-40 kHz | experimental | requires `--allow-experimental` |
| SPI > 40 kHz | unsupported | refused |

Tier thresholds are ≥10 / ≥5 / ≥2.5 samples per bit or clock cycle at the
fixed 100 kS/s capture rate. Promoting a conditional or experimental tier
requires measured error rates from the hardware fixture, not code changes
alone.

## Hardware compatibility matrix (to fill)

| Firmware | Windows | macOS (Intel/AS) | Linux | Multi-device | Hot unplug | 8-24 h soak |
|---|---|---|---|---|---|---|
| 1.1.0 | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ |
| 1.2.0 | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ |
| 1.2.4 | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ |

## Post-0.1.0 direction (not commitments)

### 0.2.x

- **Decimation on export and measure** emitting mean, min, and max per
  bucket — the ecosystem's most requested capability, answered without
  pretending the acquisition rate is configurable.
- **Absolute-time columns** anchored to the capture's `created_utc`.
- **Uncertainty-aware statistics**: per-range sample occupancy and error
  bounds derived from Nordic's per-range accuracy, so a CI threshold can be
  defended ("mean 142 uA ± 2.1 uA" rather than a bare number). No PPK2 tool
  has ever emitted an error bar.
- **Streaming offline analysis** so hour-scale artifacts can be measured and
  exported without loading them whole.
- **Firmware fingerprint matrix**: key the compatibility matrix on the
  observable fingerprint already recorded in every capture.
- **Battery-life estimation** from a measured duty cycle, with its
  assumptions stated.

### Later

- Official `.ppk2` import/export compatibility layer, explicitly lossy (the
  vendor format cannot carry raw words, ranges, or counters).
- Additional low-speed decoders: bit-banged I2C, PWM, Manchester/NRZ, GPIO
  event markers, 1-Wire.
- Optional local stdio MCP server once the core API is frozen.
- Optional Rust acceleration for parsing and edge scans if profiling
  justifies it.
- Sample-clock tolerance measured against a disciplined reference during the
  soak gate; until then the nominal 100 kS/s is assumed, not verified.

### Explicitly not adopted

- **A configurable acquisition rate.** The hardware samples at 100 kS/s,
  full stop. Decimation answers the real need; adopting the vocabulary would
  imply something false.
- **Smoothing applied to the reported series by default.** The raw
  calibrated series is the evidence; filtering is available explicitly
  (`--filtered`) and never replaces it. Expected differences between tools
  are documented in `docs/faq.md`.
- **GUI concerns** (tooltips, axis behavior, themes): no data-path analogue.
- **Automatic DUT power for convenience**: forbidden by the safety contract.
- **Higher decoder rate tiers without fixture data**: promotion requires
  measured error rates, not optimism.

## Compatibility policy

Until `0.1.0`, everything may change. From `0.1.0`, the JSON `schema_version`
only changes with a documented migration note in CHANGELOG.md, and the
capture `format_version` is append-only: newer readers open older artifacts;
older readers refuse newer ones explicitly.
