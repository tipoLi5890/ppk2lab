# CLI reference

Every command accepts the global flags `--json` (emit the envelope contract)
and `--simulate` (use the built-in simulated PPK2), before or after the
subcommand. `PPK2LAB_SIMULATE=1` is equivalent to `--simulate`. Exit codes
are documented in `docs/SPEC.md`. `ppk2lab capabilities --json` is the
machine-readable version of this page, generated from the live parser.

Durations (`--duration`, `--pre`, `--post`, `--trigger-timeout`,
`--stream-check`) accept `us`, `ms`, `s`, `min`, `h`, or a bare number of
seconds.

## discover — read-only

```bash
ppk2lab discover [--json]
```

Lists PPK2 devices with serial numbers and ports (roles: `measurement`,
`shell`, `unknown`). An empty list is not an error (warning + remediation).

## info — read-only

```bash
ppk2lab info [--device SERIAL | --port PATH] [--json]
```

Device identity, state (mode, VDD), parsed metadata/calibration, any
calibration ranges that are missing, and `firmware_fingerprint`.

The PPK2 reports no firmware version over the measurement port, so
`firmware_fingerprint` identifies it by what is observable: `hw`, `ia`, the
set of metadata keys present (`metadata_keys`, `metadata_key_count`), and
`port_count`. It is the key every compatibility-matrix row is filed under.
`port_count` is `null`, never `0`, when the device was opened with `--port`
on a path discovery did not enumerate — unknown must not read as zero, or
the same unit would file two rows that never match.

## capabilities — read-only

Full machine-readable surface: device limits, commands (each with a
`description`, a `category`, its `state_changing` classification, and every
option's `flags`/`help`/`required`/`choices`), `global_options`, decoder
tiers, exit codes, and the error, warning, gap-reason and
interruption-reason catalogs, plus the schema list.

`choices` are stringified so the list has one type (`--spi-mode` is
`["0","1","2","3"]`). The per-subcommand copies of `--json`/`--simulate` are
not listed per command; they appear once under `global_options`.

`gap_reasons` and `interruption_reasons` are **open** catalogs in the same
`{code, category, meaning}` shape as `warning_codes`, so one reader handles
all three. A new reason is a new way the hardware or host can lose samples;
treat an unknown code as unknown rather than as invalid. `category` says
where the problem lives (`capture integrity`, `measurement trust`, `device
state`, `analysis`; for gaps, where the loss happened) and is deliberately
not a severity — how much a warning matters depends on the question being
asked.

## schema — read-only

```bash
ppk2lab schema --list
ppk2lab schema envelope        # prints the raw JSON Schema
```

## doctor — read-only by default

```bash
ppk2lab doctor [--device SERIAL] [--stream-check 1s] [--json]
```

Checks Python, pyserial, enumeration, device selection, device open,
metadata, the firmware fingerprint, and calibration. `--stream-check` opts
into a short measurement (start/stop only — never DUT power) verifying the
100 kS/s rate and gap-free streaming.

**`doctor` exits nonzero when a check fails**, so `ppk2lab doctor --json ||
exit` works as a pre-flight gate. Every check carries its own `exit_code`
and the process exits with the *first* failing check's, because the checks
run in dependency order:

| check | exit code on failure |
|---|---|
| `python_version`, `pyserial` | 2 |
| `devices_found` | 3 |
| `port_open` | the underlying error's own code (4 busy/permission, 3 not found, 5 protocol, 9 otherwise) |
| `metadata_read` | 5 |
| `stream_rate` | 6 |

`warn` and `skip` never block: their `exit_code` is `0`. `device_selection`
warns when more than one device is attached and neither `--device` nor
`--port` was given — the run is still valid evidence, but it is evidence
about one unit and the report has to name it.

The result also carries `firmware_fingerprint` (same value as `info`) and
the effective `exit_code`.

## configure — state-changing (dry-run by default)

```bash
ppk2lab configure --device SERIAL [--mode ampere|source]
                  [--voltage-mv MV] [--max-voltage-mv MV]
                  [--dut-power on|off] [--apply]
```

`--max-voltage-mv` (or `PPK2LAB_MAX_VOLTAGE_MV`) sets a session ceiling below
the device limit, for a DUT that would be damaged above a known level; a
request above it is refused before any byte reaches the wire.

Without `--apply` nothing touches hardware; the result shows the projected
state. With `--apply`, each change reports requested/before/after and
readback status. Out-of-range voltage exits 8
(`VOLTAGE_OUT_OF_RANGE`) before any byte reaches the device.

## capture — measurement (never enables DUT power)

```bash
ppk2lab capture --device SERIAL (--duration 5s | --samples N | --trigger SPEC)
                [--output run.ppk2a] [--overwrite] [--digital D0-D7]
                [--pre 100ms] [--post 1s] [--trigger-timeout 30s]
                [--trigger-hold N] [--allow-experimental] [--spi-* ...]
                [--assume-voltage-mv MV] [--in-memory]
```

`--assume-voltage-mv` supplies the DUT supply voltage the meter cannot
measure, which is what makes energy computable for an Ampere-mode capture;
the result records it as an explicit assumption. Without `--output` the
capture is buffered in RAM and is refused beyond 60 s unless `--in-memory`
confirms that is intended.

The result also carries a `timeline` block cross-checking the sample
timeline against the wall clock; a capture that advanced far slower than
100 kS/s lost samples the 6-bit counter could not report and is marked
incomplete (exit 6).

Trigger specs: `current>10mA`, `current<5uA`, `digital D3 rising`,
`digital mask=0x0f value=0x05`, `uart D0 9600 "BOOT"`, `spi 0x9f,0x00`
(SPI channel config via `--spi-sclk` etc.). Incomplete captures (gaps,
interruption, trigger timeout) preserve partial data and exit 6.

Durations accept `us`, `ms`, `s`, `min`, `h`, or a bare number of seconds:
an 8-hour soak is `--duration 8h`.

## inspect — offline

```bash
ppk2lab inspect run.ppk2a [--json]
```

Reads the capture's `manifest.json` and nothing else: identity,
configuration, the timeline block, sample and chunk counts, gap count with
`gaps_truncated`, `complete`, the stored `stats` block, and the recorded
warnings. No sample chunk is opened, so the cost is independent of capture
length — this is how you look at a file too large for `measure` to load.

Because no chunk is read, neither the per-chunk CRC32s nor the whole-file
SHA-256 are checked: the digest is reported as `samples.sha256` alongside
`samples.sha256_verified: false`. `duration_s` comes from the enumerated gap
table, so `duration_is_lower_bound` is true when gaps of unknown size exist
or the gap table was truncated.

`inspect` exits 0 for an incomplete capture and reports the loss as
warnings — inspecting a damaged capture is exactly what it is for.

## decode — offline

```bash
ppk2lab decode run.ppk2a --uart D0 --baud 9600
                [--data-bits N] [--parity none|even|odd] [--stop-bits 1|2]
                [--invert] [--msb-first]
ppk2lab decode run.ppk2a --spi-sclk D1 --spi-mosi D2 [--spi-miso D3]
                [--spi-cs D4] [--spi-mode 0-3] [--spi-word-bits N]
                [--spi-lsb-first] [--spi-cs-active-high] [--spi-clock-hz HZ]
# common: [--output ann.jsonl] [--overwrite] [--allow-experimental]
#         [--max-samples N|none]
```

Emits annotations (JSONL with `--output`, inline otherwise) plus a kind/error
summary. Rates outside the validated tier warn (conditional), require
`--allow-experimental` (experimental), or are refused (unsupported).

## measure — offline

```bash
ppk2lab measure run.ppk2a                          # whole capture
ppk2lab measure run.ppk2a --window 0.1:0.25       # seconds
ppk2lab measure run.ppk2a --annotations ann.jsonl \
        --group-by annotation|kind|frame|transaction
# optional: --filtered            (adds spike-filtered statistics; raw is default)
#           --state-threshold 1mA  (duty-cycle split; see below)
#           --assume-voltage-mv MV (DUT supply voltage for energy)
#           --max-samples N|none   (load ceiling; see below)
```

Reports stored/missing/implausible/saturated samples with `covered_fraction`,
mean/min/max current, charge (uC) with `charge_is_lower_bound`, energy (uJ)
with its `voltage_basis` and `voltage_measured: false`, and the gaps inside
the window. Energy is `null` for an Ampere-mode capture unless
`--assume-voltage-mv` supplies the DUT's real supply voltage
(docs/energy-analysis.md).

`complete` is true only when the window is gap-free **and** populated end to
end. A window that reaches past the capture is not complete, however quiet
the gap table is: `samples.unpopulated` counts the positions that hold no
data, `charge_is_lower_bound` is true, and `W_WINDOW_UNPOPULATED` says so.

`--window` reads only the chunks the window falls in, so measuring a slice of
an hour-scale artifact costs the window rather than the file. That is a
partial read: `capture_sha256` is `null` with a `W_PARTIAL_INTEGRITY`
warning, because only the chunks touched were checked. Measuring with
`--annotations` reads the capture whole, because an annotation can point
anywhere in it, and each already carries the exact sample window it is
measured over. Passing both flags is refused (exit 2) rather than silently
ignoring one of them; filter the annotation file to the range you care about
instead.

The window block also carries:

- `current_ua.p50/p90/p99/p999` alongside mean/min/max, plus a `distribution`
  block naming the log-spaced grid they came from (`bins_per_decade`,
  `grid_min_ua`, `grid_max_ua`, `quantile_half_width_fraction`, and the
  samples that fell off either end). A mean alone cannot describe a
  duty-cycled load: on the simulated demo profile the mean is 1806.72 uA — a
  value the DUT never draws for a single sample — while `p50` sits on the
  6 uA sleep current;
- `samples_per_range`, `charge_per_range_uc`, `range_switches`,
  `range_switch_rate_hz`, `saturated_samples` and `saturated_ranges`. A
  sample pinned on the ADC's full-scale code is a ceiling, not a
  measurement, so saturation forces `charge_is_lower_bound` and emits
  `W_CLIPPED` naming the range;
- an `uncertainty` block: `mean_ua_typical`, `charge_uc_typical` and
  `energy_uj_typical` from Nordic's typical per-range accuracy plus each
  range's own resolution, and a separately-labelled `mean_ua_batch_stderr`.
  `guaranteed` is always `false`. The two are different quantities and must
  never be added — docs/SPEC.md, "Measurement uncertainty";
- `state_split`, which is `null` unless `--state-threshold` was given.

`--state-threshold CURRENT` splits the window at a current (`1mA`, `500uA`)
and reports samples, duration, mean, charge and run count for each side, so
"how long was it asleep and what did that cost" is one call. The threshold is
echoed back as `state_split.threshold_ua`, because the split is a function of
the caller's choice and unreadable without it; there is deliberately no
default. `runs` counts runs of consecutive *stored* samples in a state — a
gap ends a run rather than bridging it.

## assert — offline

```bash
ppk2lab assert run.ppk2a \
  --rule 'after uart("TX_DONE"), within 20ms, avg_current < 10uA' \
  [--rule ...] [--rules-file rules.json] \
  [--uart-rx D0] [--baud 9600] [--spi-* ...] [--assume-voltage-mv MV] \
  [--format json|junit] [--output report] [--allow-experimental] \
  [--max-samples N|none]
```

Rule DSL: `[after <event>,] [within <duration>,] <metric> <op> <value>`.
Events: `uart("TEXT"[, D2])`, `spi(0x9f, 0x00)`, `digital(D3 rising)`.
Metrics: `avg_current`/`mean_current`, `max_current`/`peak_current`,
`min_current`, `p50_current`/`median_current`, `p90_current`, `p99_current`,
`p999_current`, `charge`, `energy`. Values need explicit units (`10uA`,
`5uC`, `1mJ`). Exit codes: 0 all passed; 1 failed, or the `after` event was
not found; 6 a window could not be evaluated as written.

**Prefer `p99_current` to `max_current` for a CI threshold.** `max` is one
sample: every range switch adds a transient, so the observed maximum drifts
upward as a capture gets longer and the same firmware trips a fixed limit
purely because the run was longer. A percentile describes a fraction of the
distribution and is stable under capture length — see
docs/energy-analysis.md, "Why p99 rather than max". Use `max_current` when
the question really is about a single excursion, such as an inrush limit.

A window is evaluated as written, never trimmed to the data: `within 20ms`
over a capture that ends 5 ms after the anchor reports `incomplete`, not
`passed`. Every observation carries `covered_fraction`, `capture_end_sample`,
and — when it could not be evaluated — a `reason_code`:

| `reason_code` | meaning |
|---|---|
| `sample_gaps` | gaps overlap the evaluation window |
| `window_past_capture_end` | the window ends after the capture does |
| `window_unpopulated` | the window is not fully populated with samples |
| `metric_not_computable` | missing calibration, or an unknown source voltage |

An anchor is never matched across a discontinuity. A `uart("TX_DONE")`
pattern spanning a gap, an unsynchronized frame, or a frame error is not
evidence that `TX_DONE` was sent, and a `spi(0x9f, 0x00)` pattern spanning
two CS transactions is not evidence of one exchange. Such occurrences are
rejected, with a warning naming how many and why; if none survive, the
outcome is `no_event` (exit 1) rather than a pass on stitched evidence.

## export — offline

```bash
ppk2lab export run.ppk2a --format csv   --output run.csv  [--filtered]
ppk2lab export run.ppk2a --format vcd   --output run.vcd  [--channels D0-D3]
ppk2lab export run.ppk2a --format jsonl --output run.jsonl
# common: [--overwrite] [--max-samples N|none] [--window START:END]
# csv/jsonl only: [--decimate N | --bucket-ms MS]
```

CSV: one row per stored sample with raw current, range, counter, D0-D7,
validity, and a `gap_before_missing` marker on the first row after a gap. A
gap with no stored sample after it — one that ends the capture, or one
immediately followed by another gap — gets a row of its own: `valid=0`, the
gap size in `gap_before_missing`, and empty measurement columns, because
those values do not exist for samples that never arrived. The `records`
count reports stored samples and does not include marker rows. Filter on
`valid == 1` before loading into a dataframe.

VCD: 10 us timescale with `x` during gaps. JSONL: per-sample records with
inline gap records. Exports are derived views; the artifact remains the
evidence.

All three formats are written to a temp file beside `--output` and moved into
place, so an export that fails part-way — running out of space is the
ordinary case at raw CSV's tens of bytes per sample — leaves any previous
file at that path untouched.

### `--window START:END` — export a slice

```bash
ppk2lab export soak.ppk2a --format csv --output slice.csv --window 3600:3610
```

Bounds are seconds and either side may be empty (`--window 3600:` runs to the
end). Only the chunks the window falls in are read, so peak memory follows
the window rather than the file. Timestamps keep the artifact's own origin:
the `time_s` of a given sample is the same in a windowed export as in a full
one. The result reports the resolved `window` block
(`start_s`/`end_s`/`start_index`/`end_index`).

Two refusals are deliberate. `capture_sha256` is **`null`** with a
`W_PARTIAL_INTEGRITY` warning, because only the chunks touched had their
CRC32 checked and the manifest's whole-file digest was never verified — a
window digest published under that name would be a claim about the file that
nobody made. And a window whose *start* falls inside a gap is refused rather
than moved to the next stored sample, which would answer a different
question; the error names both edges so the caller can pick one. An *end*
inside a gap is fine: its answer is the last sample before it.

### `--decimate N` / `--bucket-ms MS` — summarized views

```bash
ppk2lab export soak.ppk2a --format csv --output soak-1s.csv --bucket-ms 1000
```

One record per timeline bucket with mean, min, max, charge, per-range
occupancy and switch count. Min and max are what preserve peaks; a bucket
mean alone does not. Opt-in and never a default: the header shares no column
name with the raw export, so a summary can never be mistaken for the series
it summarizes, and a `W_DECIMATED` warning travels with it. Every bucket
reports how many of its samples were present, missing, excluded and
saturated, and how many unknown-size gaps it spans, so a bucket over a gap
cannot pass for a full one. `records` counts buckets, and `decimation`
carries `bucket_samples`, `bucket_ms` and `buckets`. VCD is refused: it
carries only the digital lines, where a bucket has no meaning. Format details
in docs/decimation.md.

One hour of capture is roughly 19 GB of raw CSV, which is what this exists
for. Note what it does *not* do: decimation shrinks the output, not the read.
The capture is still materialized, so `--max-samples` still applies —
combine it with `--window` to bound both.

### Size before writing

Every export result carries `estimated_bytes`, `bytes_per_record`, and a
`size_basis` string naming what the estimate was measured on — a prefix of
this capture's own records rendered through the same formatter, not a
compiled-in constant, because row width depends on the currents and indexes
in the file. It is an estimate; check it before committing to a
multi-gigabyte write.

## `--max-samples` — the load ceiling on offline commands

`decode`, `measure`, `assert`, and `export` materialize the samples they
read, at roughly 8 bytes of RAM per stored sample. They therefore refuse, by
default, to load more than about 25 million samples (~250 s of capture,
~200 MB peak). Without `--window` that is the whole capture, whatever the
export format:

```bash
ppk2lab measure soak.ppk2a --max-samples none   # no ceiling
ppk2lab measure soak.ppk2a --max-samples 50000000
```

Exceeding the ceiling exits 2 with `CAPTURE_TOO_LARGE` — a distinct code
from `CAPTURE_FILE_INVALID`, because an 8-hour soak artifact is intact and
needs no repair. Use `ppk2lab inspect` to read such a file's manifest
without loading anything.
