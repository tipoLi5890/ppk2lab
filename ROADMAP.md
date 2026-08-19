# Roadmap

The project ships its first public release as `0.1.0`, only after every
release gate below passes. Version numbers before that are `0.1.0.devN`. The
project is never labeled `1.0.0` as part of this plan.

## Current status

The full planned `0.1.0` feature set is implemented, with hardware-free
tests, lint, and types green. A first hardware validation pass succeeded on
one PPK2 on macOS, confirming device metadata, automatic port probing, and a
gap-free 10 s / 1M-sample capture.

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

- Official `.ppk2` import/export compatibility layer.
- Additional decoders: low-speed bit-banged I2C, PWM, Manchester/NRZ, GPIO
  event markers, low-speed 1-Wire.
- Optional local stdio MCP server once the core API is frozen.
- Optional Rust acceleration for parsing and edge scans if profiling
  justifies it.

## Compatibility policy

Until `0.1.0`, everything may change. From `0.1.0`, the JSON `schema_version`
only changes with a documented migration note in CHANGELOG.md, and the
capture `format_version` is append-only: newer readers open older artifacts;
older readers refuse newer ones explicitly.
