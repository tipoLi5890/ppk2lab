# Roadmap

`0.4.0` is the current release; `0.2.0` was the project's first stable version.
Stable numbering puts the machine-readable contracts under the policy in
[docs/SPEC.md](docs/SPEC.md#stability-policy); it is not a claim that the gates
below have passed. Several need hardware this project has not had, and each
says what closing it would take. The project is never labeled `1.0.0` as part
of this plan.

This file is what is left. Completed work lives in `CHANGELOG.md`.

## Status

The planned feature set is implemented, with hardware-free tests, lint, and
types green.

One full hardware session (2026-08-20) ran the tool end to end on a single
PPK2 — `HW=49625 IA=59.0 keys=40 ports=2`, macOS on Apple silicon. It closed
the macOS pass and the absolute-time timing questions; what it measured is in
`CHANGELOG.md`. One unit, one firmware, one OS, one host, no calibrated
reference — so every other hardware gate below is still open.

## Remaining work

### Needs hardware the session did not have

- **Physical pass on Windows and on Linux** (gate 3). One PPK2 attached to
  each: `discover`, `info`, `doctor`, and a capture long enough to exercise the
  timeline. macOS exposes no USB interface numbers, so both ports reported
  `role: unknown` and the measurement port was found by the read-only metadata
  probe — the interface-number classification path has never run against
  hardware, and only Windows and Linux can run it.
- **A second unit, and a second firmware fingerprint.** Only one has ever been
  seen. That unit also reports `Calibrated: 0` while all five of its ranges
  carry constants; `doctor` warns (`calibrated_flag`) and converts anyway, and
  the flag is unexplained. A second unit is the cheapest way to learn whether
  it is a quirk of this one.
- **Decoder validation on real signals** (gate 4). Needs an MCU fixture
  emitting known content at UART 9600 baud and SPI 10 kHz, captured as golden
  artifacts, with the error rate measured against the pre-defined thresholds.
  No decoder in this project has yet seen a signal from real hardware.
- **A cross-check that can resolve gain error.** The one known load measured so
  far was a ±5% resistor, whose own tolerance is wider than the deviation being
  checked (`docs/faq.md`), so it establishes only that there is no gross error.
  Closing this needs a resistor an order of magnitude tighter, or a calibrated
  reference. (Running the same load through Nordic's official Power Profiler
  application answers a different question — whether this project's conversion
  path agrees with Nordic's, not what the instrument's error is.)
- **Hot-unplug recovery.** The cable was never pulled mid-capture.
- **8-24 h soak.** The longest capture so far is 60 s. Memory is no longer the
  obstacle; what this gate needs is bench time and disk.
- **Multi-device session.** One unit was attached; this needs two.
- **A bandwidth sweep and a range-switch settling window.** Both need hardware,
  neither gates a release; see "Blocked on a measurement" below.

### Needs no hardware

- **The web console has no server.** `0.4.0` ships the built console inside
  `ppk2lab_web/static/` and nothing that serves it: no `web` extra, no console
  script, no subcommand. Outstanding: the supervisor that owns the one open
  `PPK2` session, a `WebSocketSource` to replace the browser-side simulation
  the frontend runs against today, and evidence that a single session's
  exclusive claim on the port behaves against a real device.
- Codex plugin manifest verification against the current Codex release.
- Docs/examples reproducibility check from a clean environment (gate 2).
- Dependency license re-scan at tag time (audit in `THIRD_PARTY_NOTICES.md`).
- Release rehearsal per `docs/releasing.md`.
- PyPI publish (requires explicit maintainer authorization).

## Release gates

1. API, schemas, and capture format labeled with stability and version policy.
2. README/INSTALL/CLI examples reproducible from a clean environment.
3. At least one physical test pass on Windows, macOS, and Linux. macOS (Apple
   silicon) passed 2026-08-20; Windows and Linux are open.
4. UART 9600 and SPI 10 kHz error-rate tests meet pre-defined thresholds on
   the hardware fixture.
5. No source code copied from other projects; no unlicensed firmware
   binaries in the repository. A licensed dependency compiled into a shipped
   bundle is not "copied" here — that is gate 6.
6. All dependencies pass the license allowlist scan — Python **and** the npm
   packages bundled into `ppk2lab_web/static/`.
7. Claude Code and Codex skill/plugin installation and representative
   prompts verified.
8. CI is green on the tag commit before any release or PyPI publish, and the
   maintainer has validated the hardware gates on a physical device. Those
   gates have no CI workflow and are not meant to have one: they need a PPK2
   and an MCU fixture attached. The compatibility matrix below is the in-repo
   record of which configurations were covered.

## Decoder support tiers (frozen since `0.2.0`)

The tiers and their behaviour are in [docs/decoders.md](docs/decoders.md).
Thresholds are ≥10 / ≥5 / ≥2.5 samples per bit or clock cycle at the fixed
100 kS/s capture rate.

"Validated" names the tier's intent, not a measurement: promoting a conditional
or experimental tier — and confirming the validated one — requires measured
error rates from the hardware fixture, not code changes alone.

## Hardware compatibility matrix

Keyed on the firmware fingerprint that `ppk2lab info`, `doctor`, and every
capture manifest record, because the measurement port reports no version
string. One row per fingerprint actually attached; no row is written for a
configuration nobody ran. The matrix covers the CLI and Python paths — the web
console is not in it and cannot be until it has a server.

| Firmware fingerprint | Windows | macOS (Apple silicon) | macOS (Intel) | Linux | Multi-device | Hot unplug | 8-24 h soak |
|---|---|---|---|---|---|---|---|
| `HW=49625 IA=59.0 keys=40 ports=2` | ⬜ | ✅ 2026-08-20 | ⬜ | ⬜ | ⬜ | ⬜ | ⬜ |

That macOS pass covered: discovery and automatic measurement-port selection by
metadata probe, `info`, `doctor` (11 pass, 1 warn `calibrated_flag`, 1 skip
`stream_rate`, exit 0), 3 s and 60 s captures at 100 kS/s with all sample loss
accounted for in the gap table, recovery from SIGTERM and from SIGKILL
mid-capture, and the offline commands on the resulting artifacts. It did not
cover decoding a real signal, a calibrated reference, or anything the matrix
still shows as empty. The bench evidence itself stays with the maintainer.

## Later (not commitments)

**Blocked on a measurement.** Each needs a load or a reference this project has
not applied; the measurements it does have are in `CHANGELOG.md`.

- *Absolute-time columns.* The anchor decision shipped and the anchoring error
  is measured. What blocks publishing millisecond-precision absolute time is
  that the measurement covers one unit, one host, one OS.
- *A settling window for the range-switch filter.* `SpikeFilter.settle_samples
  = 3` is a guess; every capture so far sat wholly in range 0 with zero
  switches. Needs a load that crosses a range boundary.
- *The band a current figure is valid over.* `docs/bandwidth.md` gives the
  point-sampling model, not a measured response. Needs a square-wave load swept
  across and past the 50 kHz Nyquist frequency. RMS and any high-frequency
  statistic wait on that answer.

**Next.** The web console's server. Streaming decimation straight from an
artifact, without an in-memory capture. Battery-life estimation from the
duty-cycle split — the deliverable is the caveat framework, not the arithmetic.

**Later.** A lossy `.ppk2` import/export layer; bit-banged I2C, PWM,
Manchester/NRZ and 1-Wire decoders; an optional stdio MCP server once the API
is frozen; Rust acceleration if profiling ever justifies it; sample-clock
tolerance against a disciplined reference rather than against one host.

**Explicitly not adopted.** A configurable acquisition rate — the hardware
samples at 100 kS/s and decimation answers the real need. Smoothing by default
— the raw series is the evidence and `--filtered` never replaces it. A second
source of measurement logic — the console added in `0.4.0` reads the buckets
the library produces and issues the state changes the CLI does; it will never
compute its own answer, replace the raw artifact, or offer a route around the
preview-then-apply gate. Automatic DUT power, forbidden by the safety contract
and impossible anyway, since VOUT de-energizes within half a second of the USB
host going away. Higher decoder tiers without fixture data.

## Compatibility policy

The normative rules are in
[docs/SPEC.md](docs/SPEC.md#stability-policy): from `0.2.0` onward the JSON
`schema_version` only changes with a documented migration note in
`CHANGELOG.md`, and the capture `format_version` is append-only.
