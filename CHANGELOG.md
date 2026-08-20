# Changelog

All notable changes to this project are documented in this file. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project uses semantic versioning once released.

## [Unreleased]

Nothing yet.

## [0.3.0] — 2026-08-20

A minor rather than a patch release: it adds a subcommand, two capture flags,
two published quantiles and two `run_capture` parameters. Every one of those is
an addition, so `SCHEMA_VERSION` stays `"1"` and the capture `format_version`
stays `1`; an artifact written by 0.2.0 still opens, and a 0.2.0 consumer still
reads a 0.3.0 result.

### Fixed

- **The artifact writer was producing the sample loss the capture then
  reported.** `ArtifactWriter._flush_chunk` compressed each full chunk inline,
  on the same thread that consumes the sample stream. One chunk is 1,000,000
  samples — 4 MB, ten seconds — and deflating it takes ~290 ms on a current
  laptop. The reader's queue drained nothing for that long and overflowed, so
  the bytes it dropped came back as a `host_overflow` gap immediately after
  every chunk boundary. Compression and CRC32 now run on a dedicated writer
  thread with a two-chunk bounded queue; the producer only slices the buffer
  and hands it over. A failure on that thread is re-raised on the caller's
  thread at the next `add_block()` or at `finalize()` — a chunk that could not
  be written must never leave a manifest describing samples the container does
  not hold.
- **The stream queue was bounded in reads, not in bytes.**
  `SerialTransport.read` returns `1 + in_waiting`, and `in_waiting` is small
  exactly when the reader is keeping up: an instrumented 30 s hardware capture
  averaged 77 bytes per read across 155,577 reads. A 256-item bound therefore
  held about 20 kB — 50 ms of stream — rather than the 4 MB the 16 kB read size
  suggests, so any consumer pause past 50 ms dropped samples. `StreamSession`
  now takes `queue_bytes` (4 MB by default) and accounts for the budget in
  bytes. It also publishes `peak_queued_bytes`, so a run that never approached
  the budget can say so. On the fixed writer, real captures peaked between
  33 kB and 907 kB — the second figure is 45x the old effective capacity, so
  moving compression off the thread was necessary but not sufficient on its own.

Measured on the hardware that found it (one PPK2, macOS, Python 3.14.5, DUT
power off throughout). Before: five consecutive 30 s captures each lost
46,064–75,280 samples in exactly two gaps, at timeline indices ~1,004,5xx and
~2,03x,xxx, for 97.49–98.46% coverage. After: 5 x 30 s and 1 x 60 s recorded
3,000,000 and 6,000,000 samples respectively — no gaps, no dropped bytes,
100% coverage, `complete: true`, artifact sizes unchanged. The same 30 s
capture written with `ZIP_STORED` instead of deflate, and the same 30 s
capture held in memory with no artifact at all, had already both come back
gap-free; those were the experiments that isolated the cause.

The rest of this section is the result of auditing the four feature commits
above against their own claims before release. Each item is a defect the
features shipped with, found by reproducing it rather than by reading.

- **`compare` called a duty-cycled capture a single-range capture.**
  `dominant_range` weighted by sample count while the metrics it prices —
  `mean_ua`, `charge_uc` — are charge-weighted. A load that idles at
  microamps and bursts at milliamps puts 99.6% of its *samples* in the bottom
  range and 99.8% of its *charge* in the top one, so the comparison printed
  `basis: same_range` with the note "both captures stayed in range 0" — a
  false statement about the hardware — and priced the delta with the wrong
  shunt's accuracy. With opposite-sign per-range charge deltas the published
  bar came out 56× smaller than the term it had dropped. Dominance is now
  required in both samples and absolute charge, so the sentence is only ever
  printed when it is true.
- **The gain cancellation never checked that both captures came from the same
  instrument.** *k* is one physical unit's residual gain error; two units have
  independent *k*, and the tighter bar rests entirely on their being the same
  unit. `compare` now compares the two recorded serial numbers, warns
  `W_INSTRUMENT_MISMATCH` and withdraws the cancellation when they differ, and
  says the premise is unverified when either capture does not identify itself.
  `compare_stats` takes `same_instrument` for callers driving it directly.
- **`compare` dropped every `WindowStats` diagnostic.** It computed full
  statistics for both sides and then built its warning list from `gaps` and
  `interruption` only, so `W_BELOW_MEASUREMENT_FLOOR`, `W_CLIPPED`,
  `W_UNACCOUNTED_SAMPLES`, `W_GAP_TABLE_TRUNCATED` and `W_WINDOW_UNPOPULATED`
  all vanished. Two saturated captures whose true charge differed by about
  123,000 µC compared as `3000 ± 510 µC`, `complete: true`, exit 0, no
  warnings at all. Both sides' diagnostics are now surfaced, tagged with the
  path they came from, and each side reports `quantiles_at_floor`,
  `saturated_samples` and `charge_is_lower_bound` in the result body.
- **`compare --metric energy` ignored the supply voltage.** Energy is charge
  times V, and V comes from each capture's own supply. Two identical-current
  captures taken at 3000 mV and 5000 mV compared as `+66.7%` with no warning:
  the DUT drew the same current and the whole delta was the instrument's
  setpoint. An energy comparison now publishes a `voltage` block and warns
  when the two supplies differ; when energy is unavailable on both sides it
  says why instead of only "not computable".
- **`compare`'s `relative` used the signed baseline as its denominator.** This
  instrument legitimately reads below zero on an unloaded input, and against a
  negative baseline the sign inverted — a rise from −0.5 µA to −0.2 µA printed
  as `-60.00%`. It now divides by `abs(baseline)`.
- **`compare`'s human output presented the typical bar as the whole story.**
  `delta_batch_stderr` was computed and put in the JSON but never printed, and
  the "typical, per-range; not guaranteed" caveat `measure` prints had no
  counterpart. On one reproduction the typical bar was 37 µA while the batch
  stderr of the same delta was 299 µA, so a result indistinguishable from zero
  by the tool's own estimator read as an 8σ finding. Both now print.
- **`at=` scheduled actions were driven only by trigger output.** `sink` is
  reached through `trigger_engine.process(event)`, which returns nothing until
  the detector fires — so an action scheduled to *cause* the event the trigger
  waits for never ran, and with no `trigger_timeout_s` the capture never
  ended. When the trigger did fire, the pre-trigger ring flushed at once and
  the action was recorded at a timeline position digitised before it ran
  (measured: executed after raw index 61440, recorded as `fired_index=12288`).
  The combination is now refused with a `UsageError` that explains why.
- **`on_progress` reported nothing during a trigger wait** — the 90-second
  capture the callback exists for was the one case that showed nothing. It is
  now driven from the stream loops rather than from `sink`, so it reports
  while the trigger is still waiting.
- **A scheduled action that never came due was silently dropped.** A capture
  whose power-on stimulus never fired was byte-for-byte indistinguishable from
  one that scheduled nothing: no record, no warning, `complete: true`. Such an
  action is now recorded with `fired_index: null`, `fired_s: null` and a reason
  in `error`, and one `W_SCHEDULED_ACTION` is raised for the whole set.
- **A rejected `at=` leaked the artifact writer.** `_normalize_schedule` ran
  after `ArtifactWriter` had already opened its temp file and started its
  writer thread, and the raise escaped above the handler that would have
  aborted it. Twenty rejected calls in one process left twenty parked threads,
  twenty stray `.tmp` files and open file descriptors up from 4 to 24, with the
  temp files surviving process exit. Argument validation now runs before any
  resource is acquired.
- **`delay_s=float("inf")` escaped as a bare `OverflowError`** from `round()`,
  where every other malformed schedule entry produced a `UsageError` with a
  remediation. Non-finite delays are now refused with the rest.
- **`PPK2LAB_SIMULATE=1` relabelled an injected transport as simulated.** With
  the variable set, `PPK2.open(transport=..., serial_number=...)` returned
  `simulated: true` and a fabricated serial and firmware version while every
  byte still reached the real transport — and `run_capture` then wrote that
  identity into the manifest next to a real firmware fingerprint. The variable
  no longer applies to a transport the caller supplied; only an explicit
  `simulate=True` does.
- **An explicit `simulate=False` could not escape `PPK2LAB_SIMULATE`.**
  `_select_device` called `discover()` with no argument, so under the variable
  it enumerated the simulator and handed the literal string
  `simulated://SIM0001` to `SerialTransport`. It now asks for real hardware
  explicitly, which is what the documented opt-out promised.
- **The new warning `category` was stripped on the way into a manifest.**
  `as_json` rebuilt dict-form warnings as `{code, message}`, so `capture
  --json` carried the category and the stored artifact and `inspect --json`
  did not — and `load_capture(...).warnings` could hold both shapes in one
  list. `as_json` is now idempotent and fills the category in from the code for
  artifacts written before the field existed.
- **`measure`'s human output never printed p5 or p95.** The distribution line
  was built from a hardcoded `p50, p90, p99` while the `<=` floor marking is
  driven by `quantiles_at_floor`, which is monotone in rank. Adding p5 below
  p50 opened a band — 5% to 50% of samples below the floor — in which the
  warning named a quantile the line did not show, so the reader was told a
  number was a bound and never shown the number. The line now follows
  `WindowStats.QUANTILE_LEVELS`, which also makes p999 visible for the first
  time.
- **`comments="sn: POD01"` wrote one `# ` line per character.** A bare `str`
  satisfies `Sequence[str]` structurally, so mypy accepted it and
  `write_comments` iterated it, destroying the provenance in the file that
  exists to carry it. A bare string and a non-string element are both refused
  now. Comment lines are also CRLF-terminated to match `csv.writer`'s dialect;
  a bare LF left a file whose preamble and data rows ended differently.
- **Nothing was ever fsynced.** `atomic_write` and `ArtifactWriter.finalize`
  both renamed a complete temp file into place without flushing it to stable
  storage first, and `os.replace` is atomic for the directory entry only. A
  host losing power seconds after a capture finished could leave a
  full-length `.ppk2a` with a zeroed tail — a file the operator was told had
  been written, failing its own SHA-256. The data handle is now synced before
  the rename and the parent directory after it, for both derived exports and
  the canonical artifact.
- **A stream queue budget smaller than one read discarded everything.**
  `_reserve` had no case for a single read larger than the whole budget, so it
  refused forever, the parser was never told, and the idle timeout raised
  "the device stopped streaming" — blaming the instrument for a host-side
  configuration fault. An empty queue now admits a read whatever its size. Not
  reachable with the shipped defaults; the misattribution was the problem.

### Behaviour changes — read before upgrading a CI job

- **Captures longer than ten seconds now usually complete.** They used to
  cross a chunk boundary, lose samples there, and report `complete: false`
  with exit code 6. The same capture now reports `complete: true` and exit 0.
  A job that treated exit 6 as its normal outcome, or that pinned
  `covered_fraction` below 1.0, will see different numbers. Nothing about the
  meaning of `complete`, `covered_fraction`, or exit 6 changed — the captures
  did.
- **`assert` rules over a whole capture become evaluable.** A gap anywhere in
  the evaluation window still yields `incomplete` and exit 6; that policy is
  unchanged and is the right one. It simply stopped firing on every run
  longer than ten seconds. Rules that had never returned a verdict will now
  return one, and it may be `failed`.
- The `0.2.0` loss measurements recorded below stand as observations of that
  release. Their periodic component is now attributed to this bug — a 60 s
  capture crosses five chunk boundaries and lost exactly five whole-chunk
  gaps. What the host itself costs under load was never separated from the
  writer's stalls and is currently unquantified.

- **`W_SAMPLE_GAPS` no longer fires for an interruption.** `decode` and
  `measure` derived it from `not capture.complete`, so a capture that was cut
  short but lost nothing was reported under a code that names sample gaps.
  Each now emits `W_SAMPLE_GAPS` for actual gaps and `W_INTERRUPTED` for an
  interruption, and says how many gaps there were. A caller branching on the
  code rather than reading the prose was being told the wrong thing.
- **`complete` had four independent derivations** — the live result, the
  in-memory capture, the stored manifest, and window statistics — that agreed
  only by coincidence. The three capture-level ones now share
  `ppk2lab.capture.model.capture_is_complete`. `WindowStats.complete` answers
  a different question, about a chosen window, and stays separate on purpose.

### Added

- **Captures can say what they are of.** `capture --tag KEY=VALUE` (repeatable)
  and `run_capture(tags={...})` record provenance — board serial, firmware
  build, experiment id — in the manifest as `user_tags`, returned unchanged by
  `inspect --json`. ppk2lab never interprets a tag. Values must be strings and
  are never coerced: a tag reading `"3.7"` when `3.7` was passed would be a
  quiet lie about the record. Raw samples are the source of truth, so what a
  capture is of belongs in the capture rather than only in a filename or a
  spreadsheet beside it.
- **Actions can be scheduled inside a capture.** `run_capture(at=[(delay_s,
  callable[, label])])` fires each between two sample blocks, on the capture's
  own thread, and records the sample index it actually fired at in
  `scheduled_actions` — in the result, in the manifest, and in `inspect`. This
  is how a cold-boot inrush is recorded: the capture must already be running
  when power arrives. A timer thread would land within a scheduler quantum and
  would write to the same serial port the reader is draining; this does
  neither. A callable that raises does not cost the capture — the failure is
  recorded against the action and `W_SCHEDULED_ACTION` reports it. So is an
  action the capture ended before reaching, with `fired_index` and `fired_s`
  null: a stimulus that never happened is a fact about the run. The callable
  runs on the sample-consuming thread, so it must return well inside the
  stream buffer's depth; combining `at=` with a trigger is refused, because a
  triggered timeline starts before the trigger fires and a delay measured from
  the first sample cannot be honoured. Deliberately Python-only: `capture`
  never enables DUT power and has no option that would.
- **`run_capture(on_progress=...)`** reports `{stored, elapsed_s, gap_count,
  sample_limit}` at most every 250 ms, so a caller running a 90 s capture can
  show something moving without guessing from a wall clock. A callback that
  raises is disabled for the rest of the capture and reported once as
  `W_PROGRESS_CALLBACK`; reporting is a courtesy and is not worth a recording.
  It reports while a trigger is still waiting, which is the case it exists for.
- `StreamSession.peak_queued_bytes`, the high-water mark of buffered stream
  bytes. `ppk2lab.session` is internal (`docs/api-baseline.md` section 5); this
  is diagnostic surface, not a contract.
- Warning codes `W_SCHEDULED_ACTION`, `W_PROGRESS_CALLBACK` and
  `W_INSTRUMENT_MISMATCH`. The catalog is open, so this is an addition rather
  than a breaking change.
- `user_tags` in the capture result, not only in the manifest and in
  `inspect`: a caller that just tagged a capture should not have to reopen the
  file to read the tags back.
- **`ppk2lab compare BASELINE CANDIDATE`** — the difference between two
  captures on one metric, with an error bar that says whether the instrument's
  gain error cancelled. `ROADMAP.md` had this waiting on "the uncertainty
  surface being stable enough that a delta means something on a ±10%
  instrument"; that precondition turned out to be answerable from the existing
  model rather than to need a new one. The ±10% is a *per-range gain* error —
  the same fraction of reading on every sample through that shunt, which is
  why `TypicalUncertainty` says it does not shrink with capture length. Two
  captures through the same shunt share the unknown factor k, so
  `delta_measured = k * delta_true` and the gain contributes
  `accuracy * abs(delta)` rather than `accuracy * (abs(a) + abs(b))`. On a
  54 µA difference between two ~200 µA readings that is ±6 µA instead of
  ±45 µA. The claim is about the shunt, so `basis` is `same_range` only when
  both captures really stayed in one range — 99.5% of valid samples *and*
  99.5% of absolute charge, because the metrics being differenced are
  charge-weighted and a sample count is not. `cross_range` adds the two gains
  and says it is no tighter than the absolute figures, and `mixed` — either
  capture switched ranges — adds them conservatively. The resolution term is
  always added: it bounds an offset per sample rather than scaling a reading.
  The claim is also about one physical unit, so the two captures' serial
  numbers are compared: different units withdraw the cancellation and raise
  `W_INSTRUMENT_MISMATCH`, and unidentified ones keep it but say the premise is
  unverified. Metrics the model does not price (percentiles, `max_current`,
  `min_current`, `energy`) are still differenced, with `uncertainty: null` and
  a note saying why. Both sides report their own `complete`,
  `covered_fraction`, `user_tags`, `device`, `quantiles_at_floor`,
  `saturated_samples`, `charge_is_lower_bound` and voltage basis, and both
  sides' `WindowStats` diagnostics are surfaced as warnings, so comparing
  against a lossy, clipped or floor-served capture is visible rather than
  silent. `relative` divides by `abs(baseline)`. New schema `compare-result`;
  new `ppk2lab.analysis` exports `compare_stats`, `dominant_range`,
  `summarize_side`.

- **`p5_current` and `p95_current`.** The bins were already there; only four
  quantiles were published. p5 and p95 are the conventional floor and burst
  statistics in power work, and a `p95` column that had to be computed by
  hand from `currents_ua()` was the reason one integration materialised three
  million Python floats per capture. A fixed published set rather than an
  arbitrary `pN` keeps the rule that everything except `--state-threshold` is
  collected unconditionally, so an offline measurement of a window can never
  disagree with what that window recorded live. Both are registered as
  quantiles, so the `metric_at_measurement_floor` protection covers them.
- **Assertion observations carry `quantiles_at_floor` and
  `below_grid_fraction`.** `regression.md` had told users to run `measure`
  first to find out whether a low-current quantile rule was measuring anything
  or reading the grid floor; that was a workaround for a missing field. It is
  now in the assert report itself.
- **`export --format csv --comment TEXT`** (repeatable) and `comments=[...]`
  on `export_csv` / `export_decimated_csv` write `# ` provenance lines before
  the header. Hand-rolling the preamble meant opening the file yourself and
  calling the unfrozen `write_*` helpers, giving up the atomic write and the
  row-count check. Refused for VCD and JSONL rather than dropped: neither has
  a `# ` comment line, and a caller who asked for one would otherwise believe
  the file was annotated.

### Changed

- **`Diagnostic.to_json()` now carries `category`.** The classification
  already existed in `WARNING_CATEGORY`, but only in the `capabilities`
  catalog, so every consumer had to fetch that and join on the code just to
  learn which part of a result a warning was about. It is deliberately still
  not a severity — how much a warning matters depends on the question being
  asked.
- **`PPK2LAB_SIMULATE` is honoured by the library, not only the CLI.**
  `PPK2.open()` and `discover()` take `simulate: bool | None = None` and read
  the variable when it is left unset; passing `True` or `False` still decides
  explicitly. `PPK2LAB_MAX_VOLTAGE_MV` already worked this way, and a script
  behaving differently from the equivalent command under the same variable was
  a trap rather than a policy. It never applies to an injected `transport=`:
  the environment must not decide the identity of a transport the caller
  supplied.
- **`measure`'s distribution line now prints all six published quantiles.**
  It followed a hardcoded `p50, p90, p99`; it now follows
  `WindowStats.QUANTILE_LEVELS`, so p5, p95 and p999 appear and each can carry
  the `<=` marking that says a value was served from the grid floor.
- **Warnings carry `category` everywhere**, including inside stored manifests
  and in `inspect --json`, not only in live results.
- The byte-bounded stream queue also makes throughput steadier, not just
  loss-free. Ten interleaved 3 s full-rate simulated captures per side: before,
  the worst run achieved 77,106 S/s and one in ten reported a rate deficit over
  1%; after, the worst achieved 99,991 S/s and none did. The buffer now absorbs
  an ordinary host hiccup instead of turning it into a reported deficit.

## [0.2.0] — 2026-08-20

The project's first stable release. `0.1.0.dev0`, published on 2026-08-19,
carried only the initial implementation; everything below is new since it.

Stable numbering here means the machine-readable contracts are now under the
stability policy in `docs/SPEC.md`. It does **not** mean every release gate in
`ROADMAP.md` has passed: the hardware validation is partial — one unit, macOS
only, and no decoder has yet read a real signal. `ROADMAP.md` says which gates
are outstanding and what closing each would take.

`format_version` stays `1` and `SCHEMA_VERSION` stays `"1"`. Every new field is
an addition, and the values that change below changed only where they were
affirmatively wrong — a bug fix, not a change of meaning.

The first full hardware session ran on 2026-08-20: one PPK2, firmware
fingerprint `HW=49625 IA=59.0 keys=40 ports=2`, macOS on Apple silicon, no
calibrated reference. (The 2026-08-19 pass recorded under `0.1.0.dev0` covered
metadata, port probing and one 10 s capture, and no more.) Every figure below
attributed to hardware comes from that session; nothing here is estimated. What
the session could not establish — a second unit, a second OS, a real signal
through either decoder, an accuracy reference — is listed in `ROADMAP.md`.

### Behaviour changes — read before upgrading a CI job

The release audit, and then the hardware session, found several places where
the tool reported a conclusion the evidence did not support. Fixing them means
results that used to be green can now say, correctly, that they cannot be
evaluated. Exit code 6 (capture incomplete) is the usual new outcome, and every
case names its reason.

- **DUT power does not outlive the process that enabled it.** Measured against
  a 680 kΩ load, varying only how long the serial port stayed closed between
  enabling power and measuring: at 0 ms the output was still live 3 times out
  of 3, at 100 ms 1 of 3, at 250 ms 2 of 3, and from 500 ms out to 4 s, 0 of 3
  every time. The device de-energizes VOUT once the USB host goes away — a
  fail-safe in the instrument, and the same principle ppk2lab applies on its
  own side. The consequence is that `configure --dut-power on --apply`
  **cannot** leave a DUT powered for a later, separate `capture`: the result
  says `dut_power: true`, and that stops being true about half a second after
  the command exits. It now warns `W_DUT_POWER_TRANSIENT` instead of making a
  promise it cannot keep. A powered measurement has to happen inside one open
  session — `ppk2lab.PPK2` in Python — and should be verified from the current
  itself, since this hardware cannot report its power state back. This was
  first mistaken for an intermittent bug in `configure`, which reports exit 0
  and `applied: true` every time; whether the DUT was still powered a moment
  later came down to how fast the next process reopened the port.
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
  `metric_not_computable`. On hardware this is routine rather than exotic: a
  suite run against a 60 s capture that lost 1.08% of its samples to the host
  returned `incomplete` with `reason_code: "sample_gaps"` and
  `covered_fraction: 0.9892`.
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

### Measured on hardware (2026-08-20)

Facts about the instrument and the host that no code change here caused, and
that change how the output should be read. One unit, one host: they are
measurements of this configuration, not specifications.

- **Sample loss over USB is a property of the host, and it is not small.** Over
  60 s at 100 kS/s with the host idle: 25,792 samples missing (0.43%) in 5
  gaps. Under 12 CPU spinners and continuous disk writes: 65,024 (1.08%), also
  in 5 gaps — under load the gaps got *larger* (up to 23,104 samples), not more
  numerous. A third 60 s capture later in the same session lost 304,847 (5.1%),
  so the rate tracks whatever else the machine is doing. Every gap was
  `host_overflow`: the host's queue dropped whole chunks. In each case
  `rate_check` stayed `ok` with `unaccounted_samples_estimate` near −230, which
  is correct — the loss was fully accounted for in the gap table, so the
  wall-clock witness had nothing to add. That witness exists for the loss the
  6-bit counter cannot see, which this was not.
- **The sample clock, against this host.** Across 3 s and 60 s captures the
  anchoring error is a fixed ≈ −2.27 ms (−2.290 ms at 3 s; −2.253 ms and
  −2.26 ms at 60 s), not a proportional drift; with it removed the device clock
  agreed with this host to within ~10 ppm. So `achieved_sample_rate_hz` read
  100076 Hz from the 3 s capture and 100004 Hz from the 60 s one: a short
  capture is dominated by the fixed offset and is not a way to measure the
  sample rate. `anchor_uncertainty_s` reported 0.00016 s, which is the first
  block's span — what that field documents, and a different quantity from the
  end-to-end offset. `unaccounted_floor_samples` (10,182 over 3 s, 13,032 over
  60 s) is still dominated by the assumed `ANCHOR_JITTER_S = 0.05`; one unit on
  one host is not grounds for narrowing a safety floor, so nothing was
  tightened.
- **A unit can report `Calibrated: 0` and still carry calibration.** This one
  does: the flag is clear while all five ranges hold constants. `doctor` warns
  (`calibrated_flag`) and conversion proceeds. Unexplained, and recorded rather
  than explained away.
- **The shunt drops voltage the DUT never sees.** 5.55 µA through this unit's
  1000.625 Ω R0 costs 5.44 mV, so a load at a 3700 mV setpoint actually saw
  3.6946 V — 0.147% low. Source-mode energy is computed from the setpoint, not
  from what the DUT saw, which is why every result carries `voltage_basis` and
  `voltage_measured: false`.
- **Known-load cross-check.** A 680 kΩ ±5% resistor between VOUT and GND at
  3700 mV read 5.55 µA (5.5280–5.5614 µA over eleven captures) against the
  5.4332 µA predicted for a series circuit through that same R0 — +2.1%. But
  ±5% puts the true current anywhere in [5.175, 5.719] µA, so this establishes
  that there is no gross error and **nothing about the instrument's accuracy**;
  resolving that needs a resistor an order of magnitude tighter or a calibrated
  reference. 100% range 0, zero range switches, zero saturated samples.
- **Interruption and recovery.** SIGTERM mid-capture preserved 357,888 samples
  in a readable artifact, with `interruption.reason = "keyboard_interrupt"`,
  exit 6, and no orphan temp file — the reason string is imprecise, since a
  SIGTERM from a process manager is not an operator's keyboard; known, not
  fixed. After SIGKILL, the next open discarded 17,412 stale stream bytes and
  read metadata cleanly, and `doctor` reported `session_recovery` as a warning
  carrying the exact count.
- **`doctor` on this unit**: 11 pass, 1 warn (`calibrated_flag`), 1 skip
  (`stream_rate`, opt-in), exit 0.

### Added

- **An assertion says when its metric is only a bound.** A quantile served
  from the distribution grid's floor bounds the true value from above rather
  than measuring it, so a `p50_current` threshold could pass or fail on the
  floor itself with nothing in the report to say so. Observations now carry
  `metric_is_upper_bound`, and the verdict is reported only when it holds for
  every smaller true value — `p99_current < 1mA` passing and
  `p99_current > 1mA` failing both do; the two comparisons that would flip
  become `incomplete` with `reason_code: "metric_at_measurement_floor"`.

- **`ppk2lab inspect CAPTURE.ppk2a`** — reads the manifest only and never
  touches a sample chunk. There was no way to look at a capture without
  materializing every sample, so a file longer than about 250 s returned
  nothing at all. Measured on a 6,000,000-sample artifact: 0.126 s, no chunk
  opened.
- **`--max-samples N|none`** on `decode`, `measure`, `assert`, and `export`,
  with the new `CAPTURE_TOO_LARGE` error (exit 2) replacing a message that told
  an agent a valid soak capture was corrupt. On a whole-file load the message
  scales its units to the size being refused and names the ceiling that was
  exceeded. The windowed refusal (`--window` together with `--max-samples`)
  still renders fixed `h`/`GB` units — a 15,000-sample window reads
  "(0.0 h) ... roughly 0.0 GB of RAM" — and does not name the ceiling; that
  half of the fix is not done.
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
  `W_MANIFEST_IMPLAUSIBLE`, `W_DUT_POWER_TRANSIENT`.
- **A closable stream.** `with device.stream(...) as events:` is now the
  documented idiom. A named iterator held alive by a stored traceback kept the
  device claimed and the hardware measuring; an iterator created and never
  started leaked the claim permanently. The release is guarded by a weakref
  identity check so a spent iterator cannot release a later stream's claim.
  Confirmed on hardware: abandoning a stream mid-`with` released the claim and
  the same handle captured again immediately.
- **Windowed reads.** `ppk2lab.capture.read_window()` / `Capture.load_window()`,
  `export --window START:END`, and `measure --window` now read only the chunks
  the window falls in, so peak memory follows the window rather than the file.
  Measured on a 6,000,000-sample artifact: 10.7 MB peak for a 0.5 s window
  *and* for a 5 s window — flat, because the floor is one 1,000,000-sample
  chunk, which is CRC-checked whole before it can be sliced — against 35.7 MB
  to read the whole file. A window keeps the capture's own time base: one
  starting at 10 s reports `time_s` from 10.000, not 0.000. Three deliberate
  refusals: `capture_sha256` is `null` with a `W_PARTIAL_INTEGRITY` warning,
  because only the chunks touched were checked; `gaps_truncated` travels with
  the window; and a window whose start falls inside a gap is refused rather
  than moved, since moving it would answer a different question. Not offered on
  `decode` or `assert` — a decoder carries sync state across block boundaries,
  so a windowed decode is not a slice of a full decode.
- **`estimated_bytes` / `bytes_per_record` / `size_basis` in export results**,
  measured on a prefix of this capture's own records rather than a constant, so
  a caller can decide before committing to a multi-gigabyte write. On real
  capture data that came to 52.1 bytes per raw sample and 130.9 bytes per
  decimated bucket.
- **`export --decimate N` / `--bucket-ms M`** — one record per timeline bucket
  with mean, min, max, charge, per-range occupancy and switch count. Min and
  max are what preserve peaks; a bucket mean alone does not. Opt-in only and
  never a default: the header shares no column name with the raw export, so a
  summary can never be mistaken for the series it summarizes. Every bucket
  reports how many of its samples were present, missing, excluded, saturated,
  and how many unknown-size gaps it spans, so a bucket over a gap cannot pass
  for a full one. On a 60 s hardware capture, `--bucket-ms 100` produced 600
  buckets of which 11 were marked `complete: 0`, and `missing_in_bucket` summed
  to exactly the capture's 65,024 lost samples; the worst bucket held 4,384
  samples with 5,616 missing. `mean_ua` is defined as
  `charge_uc / (samples × 10 µs)` over present samples, so re-aggregating
  buckets does not compound error. One hour of raw CSV is about 18.8 GB at the
  record size measured above; the decimated format is documented in
  `docs/decimation.md`.
- **Distribution statistics.** `p50`, `p90`, `p99`, `p999` alongside
  mean/min/max, from a log-spaced histogram (128 bins per decade, ±0.90%
  quantile half-width, published as `distribution.quantile_half_width_fraction`)
  accumulated in the same pass. Memory is fixed regardless of capture length.
  On the shipped demo profile the mean is 1806.72 µA — a value the DUT never
  draws for a single sample — while `p50` sits on the real 6 µA sleep current.
  New assertion metrics `p50_current` (alias `median_current`), `p90_current`,
  `p99_current`, `p999_current`: a `max_current` threshold drifts upward with
  capture length because range switches accumulate, and a percentile does not.
  **A quantile that lands on the grid floor now says so.** The grid is
  logarithmic and cannot bin a reading at or below zero, which an unloaded
  input legitimately produces: over 60 s an idle PPK2 measured mean 0.1633 µA,
  minimum −0.2477 µA, maximum 0.5867 µA (a second run: 0.1769 / −0.3356 /
  0.6306), with 69–71% of samples below the grid's 200 nA floor — so `p50` came
  back as exactly 200 nA with nothing to distinguish it from a measurement.
  Affected quantiles are named in `distribution.quantiles_at_floor`, carry
  `W_BELOW_MEASUREMENT_FLOOR`, and print with a `<=` sign. The clamp into the
  observed `[min, max]` was documented as covering this and does not: it
  rescues a floor-bin quantile only when the whole distribution sits below the
  floor, because it is the maximum that pulls the value down.
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
  bottom range is ±10% of gain plus a 0.2 µA step, so ±30%, not ±10%. On the
  unloaded unit measured above it goes further still: `mean_ua_typical` came
  back 0.2177 µA against a 0.17 µA mean — ±123%, an error bar larger than the
  reading, which is the resolution term doing its job at the bottom of range 0.
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
  beat that corrupts every per-frame energy figure. Its tables are the
  point-sampling model, not a measured response; no bandwidth sweep has been
  run on hardware.
- New FAQ entries, including why a reading can go below zero at very low load —
  now also confirmed on hardware, where negative readings at an unloaded input
  are routine.

### Fixed

- **`capabilities --json` no longer publishes a validated rate nobody has tried.** The UART tier reported `validated_max_baud: 10000` — the grid's arithmetic maximum at 10 samples per bit — while its own note, all four READMEs, the ROADMAP and every document said 9,600. An agent reading the machine-readable field would have accepted a rate no decoder has ever run at, on real hardware or otherwise.
- **A terminated capture is no longer recorded as a keyboard interrupt.** SIGTERM now yields `interruption.reason = "terminated"` with the signal number; a process manager, a CI timeout, and a person pressing Ctrl-C are different causes and only one of them is a person.
- **The windowed `CAPTURE_TOO_LARGE` message matches the whole-file one**, scaling its units and naming the ceiling instead of reporting "0.0 h" and "0.0 GB".
- **The serial transport drains before closing.** A command byte still in the
  OS write buffer has not reached the device, and closing a tty may discard
  it. Every command the PPK2 answers proves its own delivery through the
  reply — but DUT power has no readback at all, so a write lost this way would
  be reported as applied and never contradicted. (This is a real hole, closed
  defensively. It is *not* what made `configure --dut-power on --apply` look
  intermittent — that was the instrument dropping VOUT after the port closed,
  above — and closing it did not change that rate.)
- **The wall-clock witness survived neither a read nor a write.** A read/write
  round trip cut the manifest's timeline block from 11 keys to 4, and
  `capture.save()` never wrote it at all, so a 6.79% deficit reloaded as
  `rate_check: ok`, `complete: true`, `covered_fraction: 1.0`. `gaps_truncated`
  was written by the writer and read by nothing. Both now round-trip
  key-for-key, on the stored and the in-memory path alike.
- **A failed export destroyed the previous one.** All three exporters opened
  the destination directly, so a mid-write failure — running out of space is
  the ordinary case at CSV's measured 52.1 bytes per sample — left a truncated
  file that read like a complete export, and with `--overwrite` the previous
  good export was already gone. CSV was the worst of the three: its row-count
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
  exception already in flight and still raises when nothing is. (Not yet
  exercised on hardware: no cable has been pulled mid-capture.)
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

#### Added

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

#### Fixed

Auditing the hardening above against the hardware facts it claims to enforce
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

#### Changed

- Agent skills, `docs/agent-interface.md`, and `capabilities --json` now
  carry the contract they describe: the warning catalog is published so an
  agent can learn what each `W_*` code means from the tool, and the skills
  cover `--assume-voltage-mv`, `--max-voltage-mv`, `timeline.rate_check`,
  and `--in-memory`.
- Codex install instructions describe what exists (loose skills) rather than
  a plugin manifest that is not published yet.
- The simulator defaults to Source Meter mode, matching what real hardware
  reported during validation (`mode: 2`) — and still does: the unit in the
  2026-08-20 session was found in `source` mode.

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
