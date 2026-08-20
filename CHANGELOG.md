# Changelog

All notable changes to this project are documented in this file. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project uses semantic versioning once released.

## [Unreleased] — 0.2.0.dev0

`0.1.0.dev0` was published to PyPI on 2026-08-19 and carries only the initial
implementation; everything under this heading is unreleased. The first stable
release will be `0.2.0`, cut once every gate in `ROADMAP.md` passes.

`format_version` stays `1` and `SCHEMA_VERSION` stays `"1"`. Every new field is
an addition, and the values that change below changed only where they were
affirmatively wrong — a bug fix, not a change of meaning.

### Behaviour changes — read before upgrading a CI job

The release audit found several places where the tool reported a conclusion the
evidence did not support. Fixing them means results that used to be green can
now say, correctly, that they cannot be evaluated. Exit code 6 (capture
incomplete) is the usual new outcome, and every case names its reason.

- **A window that is not populated is no longer `complete`.** `complete` meant
  "no gap event fell inside this window", so a 10-second window over a 50 ms
  capture returned `complete: true`, `charge_is_lower_bound: false` and no
  warnings — 0.5% of the data, reported as the whole answer. It now means what
  it says: gap-free *and* populated.
- **An assertion window is evaluated as written, not trimmed to the data.**
  `within 20ms` over a capture that ends 5 ms after the anchor used to be
  silently clamped to the capture end and reported `passed`. It now reports
  `incomplete` with `reason_code: "window_past_capture_end"`, and every
  observation carries `covered_fraction` and `capture_end_sample`. The other
  reason codes are `sample_gaps`, `window_unpopulated`, and
  `metric_not_computable`.
- **UART frames decoded before a confirmed idle run are tagged `unsynced`
  with `confidence: 0.0`.** A receiver cannot know where a frame starts until
  it has seen a high run longer than any that can occur inside one; at 8N1 that
  is a whole frame time, and the decoder was re-arming after 1.5 bit times. The
  frame is still reported as raw evidence, but it never joins the byte stream,
  anchors an assertion, or fires a trigger. This covers stream start as well as
  every gap and break — so `capture --trigger` was affected on every run, with
  no gap involved. There is deliberately no option to lower the threshold.
- **`assert` no longer matches an anchor across a discontinuity.** A
  `uart("TX_DONE")` pattern spanning a gap, an unsynchronized frame, or a frame
  error is not evidence that `TX_DONE` was sent, and a `spi(0x9f, 0x00)`
  pattern spanning two CS transactions is not evidence of one exchange. Suites
  that passed on stitched evidence now return `no_event` (exit 1 — only an
  `incomplete` outcome maps to exit 6) with a warning naming how many
  occurrences were rejected and why. `capture --trigger`
  with a content pattern inherits the same rule.
- **`doctor` exits nonzero when a check fails.** It always exited 0, so
  `ppk2lab doctor --json || abort` — the obvious pre-flight reflex, and a
  required field of this project's own compatibility-report template — was a
  no-op. `warn` and `skip` stay non-blocking, and `--simulate doctor` still
  exits 0.
- **`DeviceState` is frozen and `PPK2.state` is read-only.** Writing
  `dev.state.dut_power = True` suppressed `W_DUT_POWER_UNKNOWN` *and* wrote
  `dut_power: true` into the capture manifest — a hardware claim that was never
  commanded and that the PPK2 cannot report back. Use the state-changing
  methods; direct assignment now raises.
- **CSV export emits a marker row for a gap with no stored sample after it**
  (one that ends the capture, or one immediately followed by another gap).
  Previously such a gap vanished from the CSV while JSONL still recorded it, so
  the two exports of one artifact disagreed about whether data was lost. The
  row carries `timeline_index`, `time_s`, `valid=0` and `gap_before_missing`;
  the measurement columns are empty because those samples never arrived. The
  return value and the `records` field still count stored samples only. Filter
  on `valid == 1` before loading into a dataframe.
- **`UsageError` deliberately does not inherit `ValueError`**, and
  `CaptureTooLargeError` subclasses `UsageError` rather than `CaptureFileError`
  — an 8-hour soak artifact is not an invalid file. Catch `Ppk2labError`.

### Added

- **`ppk2lab inspect CAPTURE.ppk2a`** — reads the manifest only and never
  touches a sample chunk. There was no way to look at a capture without
  materializing every sample, so a file longer than about 250 s returned
  nothing at all.
- **`--max-samples N|none`** on `decode`, `measure`, `assert`, and `export`,
  with the new `CAPTURE_TOO_LARGE` error (exit 2) replacing a message that told
  an agent a valid soak capture was corrupt.
- **Duration unit `h`.** `capture --duration 8h` parses; the 8-24 h soak gate
  was expressible only as `28800s`.
- **`firmware_fingerprint` in `info --json` and `doctor`.** It reached only the
  capture manifest before, yet it is what keys a row of the compatibility
  matrix — the measurement port reports no version string.
- **Saturation and range occupancy.** `saturated_samples`, `saturated_ranges`,
  `samples_per_range`, `charge_per_range_uc`, `range_switches`, and
  `range_switch_rate_hz`. A pinned ADC code is by construction just under 1 A,
  so the implausible-current guard could never catch it: a 2.5 A load reported
  `mean = max = 1000000.0 µA, complete: true` — a 2.5× error rendered as a
  perfectly regulated flat top. Saturation now forces `charge_is_lower_bound`
  and emits `W_CLIPPED` naming the range.
- **Wall-clock accounting in every capture**: `first_sample_utc`,
  `anchor_uncertainty_s`, a signed `rate_offset_ratio` alongside the clamped
  `rate_deficit_ratio`, `unaccounted_samples_estimate`, and
  `unaccounted_floor_samples`. The 6-bit counter cannot express a loss of 64 or
  more samples, so elapsed wall time is the only witness to that class.
- **Coded catalogs in `capabilities --json`**: `gap_reasons` and
  `interruption_reasons`, published open (a new reason is not a breaking
  change) in the same `{code, category, meaning}` shape as `warning_codes`, so
  one reader handles all three. New warning codes `W_UNACCOUNTED_SAMPLES`,
  `W_WINDOW_UNPOPULATED`, `W_GAP_TABLE_TRUNCATED`, `W_CLIPPED`,
  `W_MANIFEST_IMPLAUSIBLE`.
- **A closable stream.** `with device.stream(...) as events:` is now the
  documented idiom. A named iterator held alive by a stored traceback kept the
  device claimed and the hardware measuring; an iterator created and never
  started leaked the claim permanently. The release is guarded by a weakref
  identity check so a spent iterator cannot release a later stream's claim.
- **Windowed reads.** `ppk2lab.capture.read_window()` / `Capture.load_window()`,
  `export --window START:END`, and `measure --window` now read only the chunks
  the window falls in, so peak memory follows the window rather than the file
  (measured flat at about 10 MB for a 50k-sample window whether the artifact
  holds 2 M or 16 M samples; the floor is one 1 M-sample chunk, which is
  CRC-checked whole before it can be sliced). Three deliberate refusals:
  `capture_sha256` is `null` with a `W_PARTIAL_INTEGRITY` warning, because only
  the chunks touched were checked; `gaps_truncated` travels with the window;
  and a window whose start falls inside a gap is refused rather than moved,
  since moving it would answer a different question. Not offered on `decode`
  or `assert` — a decoder carries sync state across block boundaries, so a
  windowed decode is not a slice of a full decode.
- **`export --decimate N` / `--bucket-ms M`** — one record per timeline bucket
  with mean, min, max, charge, per-range occupancy and switch count. Min and
  max are what preserve peaks; a bucket mean alone does not. Opt-in only and
  never a default: the header shares no column name with the raw export, so a
  summary can never be mistaken for the series it summarizes. Every bucket
  reports how many of its samples were present, missing, excluded, saturated,
  and how many unknown-size gaps it spans, so a bucket over a gap cannot pass
  for a full one. `mean_ua` is defined as `charge_uc / (samples × 10 µs)` over
  present samples, so re-aggregating buckets does not compound error. One hour
  of capture is 18.6 GB of raw CSV; the format is documented in
  `docs/decimation.md`.
- **`estimated_bytes` / `bytes_per_record` / `size_basis` in export results**,
  measured on a prefix of this capture's own records rather than a constant, so
  a caller can decide before committing to a multi-gigabyte write.
- **Distribution statistics.** `p50`, `p90`, `p99`, `p999` alongside
  mean/min/max, from a log-spaced histogram (128 bins per decade, ±0.90%
  quantile half-width, published as `distribution.quantile_half_width_fraction`)
  accumulated in the same pass. Memory is fixed regardless of capture length.
  On the shipped demo profile the mean is 1806.72 µA — a value the DUT never
  draws for a single sample — while `p50` sits on the real 6 µA sleep current.
  New assertion metrics `p50_current` (alias `median_current`), `p90_current`,
  `p99_current`, `p999_current`: a `max_current` threshold drifts upward with
  capture length because range switches accumulate, and a percentile does not.
- **`measure --state-threshold CURRENT`** — duty-cycle decomposition into
  below/above, each with samples, duration, mean, charge and excursion count.
  The threshold is always echoed in the result, because the split is a function
  of the caller's choice; there is deliberately no default.
- **Typical per-range uncertainty.** `uncertainty.mean_ua_typical`,
  `charge_uc_typical`, `energy_uj_typical`, computed from Nordic's per-range
  accuracy plus each range's own resolution and summed **linearly** — these are
  systematic gain specifications, fully correlated within a range, so combining
  them in quadrature or dividing by √N would be wrong. The resolution term
  matters most exactly where low-power work lives: a 1 µA reading in the
  bottom range is ±10% of gain plus a 0.2 µA step, so ±30%, not ±10%.
  `mean_ua_batch_stderr` is a separate, separately-labelled figure from
  sub-window batches, because 10 µs samples through a shunt-switching front end
  are heavily autocorrelated and σ/√N understates the spread by an order of
  magnitude. Every field is named `..._typical...` and `guaranteed` is always
  `false`. Human output rounds to the digits the interval supports; JSON keeps
  full precision.
- **`ppk2lab.decoders.uart.uart_runs()`** — the clean byte stream split at
  every discontinuity. `uart_bytes()` is now its concatenation, so the two
  cannot drift.
- **A gap between UART frames is now visible.** The decoder emits an `error`
  annotation (`fields.reason = "sample_gap"`) spanning the missing samples even
  when no frame was in flight; previously such a gap left no trace in decoded
  output at all. `measure --group-by kind` shows them under `error`.
- **`docs/bandwidth.md`** — the frequency band over which a reported current
  figure is valid, what aliases at a fixed 100 kS/s, and which statistics
  survive it. A 100 kHz ripple moves the reported mean by +31% with no gap and
  no warning; a 99.9 kHz one leaves the mean exact while folding to a 100 Hz
  beat that corrupts every per-frame energy figure.
- New FAQ entries, including why a reading can go below zero at very low load.

### Fixed

- **A percentile at the grid floor said so.** Found on real hardware: an
  unloaded PPK2 reads below the distribution grid's 200 nA floor about 69% of
  the time (measured over 60 s: mean 0.17 µA, minimum −0.25 µA, maximum
  0.59 µA), so `p50` came back as exactly 200 nA with nothing to distinguish
  it from a measurement. The grid is logarithmic and cannot bin a reading at
  or below zero, which an unloaded input legitimately produces. Affected
  quantiles are now named in `distribution.quantiles_at_floor`, carry
  `W_BELOW_MEASUREMENT_FLOOR`, and print with a `<=` sign. The clamp into the
  observed `[min, max]` was documented as covering this and does not: it
  rescues a floor-bin quantile only when the whole distribution sits below the
  floor, because it is the maximum that pulls the value down.
- **`CAPTURE_TOO_LARGE` described a one-minute capture as "0.0 h" needing
  "0.0 GB of RAM".** The units now scale to the size being refused, and the
  message names the ceiling it exceeded.
- **The wall-clock witness survived neither a read nor a write.** A read/write
  round trip cut the manifest's timeline block from 11 keys to 4, and
  `capture.save()` never wrote it at all, so a 6.79% deficit reloaded as
  `rate_check: ok`, `complete: true`, `covered_fraction: 1.0`. `gaps_truncated`
  was written by the writer and read by nothing. Both now round-trip
  key-for-key, on the stored and the in-memory path alike.
- **A failed export destroyed the previous one.** All three exporters opened
  the destination directly, so a mid-write failure — running out of space is
  the ordinary case at ~53 bytes per sample for CSV — left a truncated file
  that read like a complete export, and with `--overwrite` the previous good
  export was already gone. CSV was the worst of the three: its row-count
  self-check *deleted* the destination outright. All three now write beside the
  target and move into place.
- **A failed rename no longer deletes a finished capture.** The temp file is a
  complete, valid, readable artifact by the time `os.replace` runs, so aborting
  there would destroy a hardware capture that cost bench time and cannot be
  re-acquired. It is preserved and its path is named in the remediation. An
  output path that is a directory is now refused before the first sample is
  recorded, rather than after the whole capture.
- **A NaN threshold in a rules file silently blocked merges.** `"value": NaN`
  returned `ok: true, passed: false` — a failed build from a comparison that
  never happened. Rule JSON now rejects a non-finite threshold, a missing
  field, a non-numeric `within_s`, and a non-object `after`.
- **JUnit `<system-out>` is JSON.** It contained `str(dict)`, which no JSON
  parser accepts — the evidence blob CI dashboards actually surface was
  unreadable.
- **Content triggers no longer fire on stitched evidence.** `UartContentTrigger`
  and `SpiContentTrigger` skipped errored and boundary annotations without
  clearing the match window, so `uart D0 9600 "DONE"` fired on
  `D, O, <corrupted frame>, N, E` and `spi 0x9f,0x00` fired across two CS
  transactions. The same guard removes a latent crash on a 9-bit UART decoder.
- **Malformed and hostile capture files.** A directory path exited 9; a FIFO
  path hung forever; a missing `format_version` was accepted silently; a
  malformed gap table exited 9. All are now typed errors with remediation. An
  out-of-bound gap index or an implausible total warns (`W_MANIFEST_IMPLAUSIBLE`)
  rather than erroring or being clamped — laundering an impossible input into a
  plausible number is what this project exists not to do.
- **A hot unplug during stream teardown could mask itself.** A failing drain in
  the `finally` replaced the real transport error; it now defers to an
  exception already in flight and still raises when nothing is.
- **An abandoned async stream kept the hardware measuring.** An `async for`
  over `AsyncPPK2.stream()` left by an exception or by `task.cancel()` held the
  device claim until the event loop finalized the async generator. `stream()`
  now returns a closable `AsyncStreamIterator`, and `async with
  adev.stream(...) as events:` is the documented idiom; bare `async for` and
  `contextlib.aclosing()` are unchanged. Two async-only hazards with no sync
  counterpart came with it: a cancellation landing inside `__anext__` used to
  leave a worker thread executing the underlying generator, which could then
  release a *later* stream's claim — the same two-readers-on-one-framing-stream
  failure the sync guard exists to prevent — and a cancelled step used to
  discard an event the worker had already fetched, which under a `wait_for`
  retry is a hole in the timeline reported as clean data.
- **A cancelled `AsyncPPK2.open()` abandoned an already-open serial port.**
  `asyncio.to_thread` cannot interrupt its worker, so the port stayed claimed
  by the process with no reference left to close it.
- `PPK2.stale_bytes_after_stop` is now `int | None`: `None` means no post-stop
  drain completed, where `0` previously meant both "the channel was verified
  quiet" and "the handle was dead and nothing could be drained".
- Uncalibrated ranges, unknown source voltages, and captures with no
  calibration raise `CalibrationUnavailableError` instead of a bare
  `ValueError`, which escaped every documented handler.

### Changed

- **Warning, gap-reason, and interruption-reason catalogs carry a `category`**
  (`capture integrity`, `measurement trust`, `device state`, `analysis`; and
  for gaps, where the loss happened). Deliberately not a severity: how much a
  warning matters depends on the question being asked.
- **`capabilities --json` is usable on its own.** Every command now publishes a
  description, `argparse.SUPPRESS` placeholders are gone, and every option that
  constrains its values publishes `choices` — an agent could not previously
  construct a single valid `export` call from it, and `configure --mode` gave
  no hint that the values are `ampere|source`.
- `measure` attaches the window's own coded findings to the result warnings.
- The statistics accumulator's hot loop now also produces per-range occupancy,
  per-range charge, saturation and range-switch counts, and the distribution
  grid, rather than paying for a second pass over every sample. Measured in two
  steps on the author's machine: occupancy, saturation and switch counting cost
  101.7 → 121.1 ns/sample (+19.1%), and fusing the grid in cost a further
  121.4 → 172.9 ns/sample (+42%), rising to 190.0 (+56%) when a duty-cycle
  threshold is also being tracked. End to end that is 101.7 → 172.9 ns/sample,
  about 1.7% of one core at the acquisition rate and roughly 62 s of CPU for an
  hour-long capture measured offline.
- `.github/workflows/ci.yml` and `docs/releasing.md` cited a hardware workflow
  that does not exist, which made release gate 8 unsatisfiable as written. The
  gate now says what is actually true: the hardware gates are run by the
  maintainer on the bench, and `ROADMAP.md`'s compatibility matrix records
  which configurations were covered.

### Packaging

- **Ships `py.typed`** (PEP 561). Without it every downstream type checker
  treated the whole surface as `Any` — demonstrated: mypy did not flag a call
  to a method that does not exist on a `PPK2`-annotated parameter. A consumer's
  CI that was passing only because ppk2lab was unchecked can go red.
- **Removed the `numpy` extra.** No code ever imported NumPy.
  `pip install ppk2lab[numpy]` keeps working (pip warns on an unknown extra)
  and installs nothing. This also closes the last open item in the dependency
  license audit: every remaining row is now verified from installed metadata.
- License metadata moved to the PEP 639 SPDX form (`License-Expression: MIT`);
  the build requirement moved with it.
- The sdist now carries `CHANGELOG.md`, `THIRD_PARTY_NOTICES.md`, `docs/`,
  `examples/`, and the policy documents.
- README links are absolute, so they resolve on the PyPI project page — 24 of
  them were 404s there.
- CI adds Python 3.14, matching the classifier the package already advertised,
  runs every example against the simulated device, and guards the wheel's
  `py.typed` and license metadata in both the CI and release builds.
- New tests pin the documentation to the code: `tests/test_api_baseline.py`
  diffs `docs/api-baseline.md` against `capabilities --json` in both
  directions, and `tests/test_docs_cli_surface.py` runs every `ppk2lab ...`
  line in `docs/`, the READMEs, and the agent skills against the real parser.

### Earlier unreleased work — measurement integrity (2026-08-19)

Committed the same day as the `0.1.0.dev0` upload but after it, so none of
this has ever been on PyPI. It ships for the first time here.

### Fixed (integrity audit of the hardening itself, 2026-08-19)

Auditing the change below against the hardware facts it claims to enforce
found that parts of it did not hold.

- **A lost byte no longer becomes invented time.** Treating the first
  counter mismatch as proof of a device-side skip meant a byte-level desync
  advanced the timeline once per shifted word: a single lost byte inflated a
  2000-sample stream to 5713 — 3715 phantom samples, 37 ms that never
  happened. A mismatch is now held until the following samples confirm the
  framing survived, and the desync verdict always arrives first. Measured
  phantom samples: 0 at realistic chunk sizes, bounded at 189 for any
  chunking. Shifted words are discarded instead of being reported as
  measurements, and unrecoverable bytes are dropped once rather than
  re-entered word by word.
- **Stored captures keep their energy.** Artifacts written before voltage
  provenance existed have no `voltage_basis`; reading them as "unknown"
  silently voided every source-mode energy figure. The recorded mode now
  stands in for the missing basis.
- **An uncalibrated device is no longer reported as starved.** Timeline
  extent was read from the statistics accumulator, which only advances when
  samples convert, so a device opened without metadata looked 100% starved
  and every capture was marked incomplete.
- **Triggered captures decline the rate check.** Pre-trigger samples are
  emitted at the fire moment, so wall time covered only the post-trigger
  window while the timeline covered both — a comparison that could read
  200 kS/s or hide a 50% loss as healthy. It now reports `not_applicable`.
- **A failed stream start no longer bricks the handle.** The device was
  claimed before `start_measuring`, so a transient write error left the
  claim set and every later command blamed a stream that never began. The
  claim is also atomic now, and made when the stream is requested rather
  than when it is first iterated.
- **An unplugged device is reported as an unplug**, not as a stall: the
  reader's real error wins over the idle budget even when the queue was full
  enough to drop its sentinel. The idle budget also measures device silence
  rather than consumer work.
- **The in-memory guard cannot be bypassed** by passing an output path, and
  the read path now refuses to load a capture too large for memory instead
  of failing after allocating it.
- `W_STREAM_DESYNC` is actually emitted; gap tables are bounded where they
  really grow; `charge_is_lower_bound` covers every excluded sample, not
  only gaps; charge uses compensated summation so sub-microamp samples keep
  contributing to an hours-long total; measuring N annotations no longer
  walks the whole capture N times; a terminated capture is preserved instead
  of left as an unreadable temp file; opening a serial port is bounded.

### Changed

- Agent skills, `docs/agent-interface.md`, and `capabilities --json` now
  carry the contract they describe: the warning catalog is published so an
  agent can learn what each `W_*` code means from the tool, and the skills
  cover `--assume-voltage-mv`, `--max-voltage-mv`, `timeline.rate_check`,
  and `--in-memory`.
- Codex install instructions describe what exists (loose skills) rather than
  a plugin manifest that is not published yet.
- CI runs every example against the simulated device.

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

## [0.1.0.dev0] — 2026-08-19

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
