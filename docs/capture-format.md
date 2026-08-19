# Canonical capture artifact (`.ppk2a`)

The capture artifact preserves raw evidence: unmodified sample words,
calibration metadata, device identity, configuration, a truthful timeline,
and every known data loss. Derived exports (CSV/VCD/JSONL) never replace it.

Format name `ppk2lab-capture`, `format_version` 1. Readers refuse newer
versions explicitly; the version is append-only.

## Container

A ZIP file (deflate-compressed members):

```text
run.ppk2a
├── manifest.json        # versioned manifest (schema: capture-manifest)
├── metadata.txt         # raw device metadata text (calibration evidence)
└── chunks/
    ├── 000000.u32       # raw little-endian uint32 sample words
    └── 000001.u32       # 1,000,000 samples (4 MB) per chunk
```

## Manifest

Validated by the `capture-manifest` schema (`ppk2lab schema
capture-manifest`). Key sections:

- `capture_id` (uuid), `created_utc`;
- `device`: serial number, VID/PID, ports, firmware (if known), `simulated`;
- `configuration`: mode, `source_voltage_mv`, `dut_power`, `sample_rate_hz`
  (100000), digital channels, the requested duration/limit, the trigger
  description for triggered captures, and the energy provenance the meter
  cannot measure for itself — `assumed_voltage_mv`, `voltage_basis`, and
  `voltage_measured` (always `false`). `dut_power` is `null` unless this
  session commanded it: the device cannot report the DUT power state back, so
  an unknown one is recorded as unknown rather than as a hardware claim;
- `timeline`: `sample_rate_hz` (required — every timestamp derives from it,
  so a reader refuses a manifest without it rather than assuming the usual
  value), `sample_period_ns` (10000), `start_index` (nonzero for triggered
  captures whose window begins mid-stream), `degraded` (true once an
  unknown-size gap occurred or the gap table was truncated), plus the
  wall-clock cross-check: `started_utc`, `ended_utc`, `first_sample_utc`,
  `anchor_uncertainty_s`, `wall_elapsed_s`, `timeline_advance`,
  `achieved_sample_rate_hz`, `rate_deficit_ratio`, `rate_offset_ratio`,
  `unaccounted_samples_estimate`, `unaccounted_floor_samples`, and
  `rate_check` (`ok` / `deficit` / `too_short` / `not_applicable`). The
  cross-check is the only evidence of sample loss the 6-bit counter cannot
  describe — a loss of exactly 64 samples, or any multiple of it, is
  invisible to the counter — and it cannot be recomputed after the fact,
  because the loss it describes is precisely what the stored samples do not
  contain. Everything in the block that is not `sample_rate_hz`,
  `sample_period_ns`, `start_index` or `degraded` therefore survives a
  read/write round trip verbatim, including keys a future version adds. The
  block itself is required: a manifest with no `timeline` is refused as
  `CAPTURE_FILE_INVALID`. A manifest that carries only the four keys above
  is read, but no wall-clock cross-check exists for it — `rate_check` is
  absent rather than `ok`, and the loss class the counter cannot describe is
  simply unwitnessed for that artifact;
- `calibration`: provenance for the numbers — `calibrated_flag`,
  `metadata_terminated`, `metadata_warnings`, `missing_ranges`,
  `user_gains`. A capture must carry the reason its readings might be
  suspect, not just the readings;
- `samples`: encoding `u32le-v1`, `stored_count`, `invalid_range_count`,
  SHA-256 over the concatenated raw sample bytes, and a chunk table with
  per-chunk `first_stored_index`, `count`, and CRC32;
- `gaps`: every known loss (`index`, `missing`, `reason`, `ambiguous`) in
  timeline order;
- `gaps_truncated`: gaps the capture counted but did not enumerate. The gap
  table stops at 10,000 entries — a desync storm can produce gaps faster than
  anyone could read them — and past that the count stays exact while the
  spans stop being recoverable. A nonzero value forces `timeline.degraded`,
  because positions after an unlisted gap cannot all be resolved. It is read
  as well as written, and rewriting a capture carries the count in rather
  than recounting truncation from an already-truncated table, which would
  reset it to zero;
- `complete`: true only when the requested capture finished without
  interruption **and** without gaps;
- `interruption`: `null` or `{reason, detail}`. The reasons are an open
  catalog, not an enum — a future one must not be a breaking change — and
  `ppk2lab capabilities --json` publishes the current list as
  `interruption_reasons`. Today: `keyboard_interrupt`, `transport_error`,
  `stream_stalled` (the port stayed open but samples stopped arriving),
  `trigger_timeout`, `trigger_never_fired`, and `timeline_compression` (the
  timeline advanced far slower than the wall clock, so loss occurred beyond
  what the 6-bit counter can report);
- `stats`: precomputed summary (window-stats schema) for quick inspection —
  always recomputable from the raw samples;
- `warnings`: coded notes from the capture run, each `{code, message}` (see
  `ppk2lab.diagnostics`), so a stored capture stays as branchable as a live
  result.

## Timeline and gaps

Stored samples are contiguous *received* samples. The mapping between the
stored index `k` and the timeline index `t` walks the gap table:

```text
t = start_index + k + sum(missing for gaps positioned at or before t)
```

Positions inside a gap have no stored sample. Tools must never interpolate
across a gap or shift later samples earlier.

## Integrity and atomicity

- Writers stream chunks (bounded memory), write to a temp file in the target
  directory, and `rename()` atomically on finalize; interrupted writes leave
  no partial artifact under the final name.
- An existing output is never replaced without an explicit overwrite flag.
- Readers verify container structure, `format_version`, sample encoding,
  chunk byte counts, per-chunk CRC32, and — when the whole capture is read —
  the manifest SHA-256 against the stored data before returning samples. A
  windowed read verifies only the chunks it touches and says so rather than
  reporting a digest it did not check.

## Reading

```python
import ppk2lab
cap = ppk2lab.Capture.load("run.ppk2a")     # verified, in-memory
for event in cap.iter_events():             # SampleBlock | GapEvent, timeline order
    ...
```

`Capture.load` materializes every sample and refuses a file larger than its
`max_samples` ceiling (`None` removes it). It streams the SHA-256 chunk by
chunk as it reads, so the digest costs no second copy of the samples, and the
verified value is available afterwards as `capture.artifact_sha256`.

### Windowed reads

One hour of recording is 360 million samples, so anything hours long has to
be read a slice at a time:

```python
cap = ppk2lab.Capture.load_window("soak.ppk2a", start_s=3600, end_s=3610)
```

Bounds are timeline indexes (`start_index`/`end_index`) or seconds from the
capture's own first sample (`start_s`/`end_s`). Only the chunks the window
falls in are read, so peak memory follows the window rather than the file.
`ppk2lab inspect` reads the manifest alone and touches no chunk at all.

Three properties are part of the contract:

- **`artifact_sha256` is `null`.** A windowed read verifies the CRC32 of the
  chunks it touches and never sees the rest of the file, so the manifest's
  whole-file digest is not checked and is not published as though it were. A
  `W_PARTIAL_INTEGRITY` warning says so. Load the capture whole to check it.
- **`gaps_truncated` travels with the window.** A capture whose gap table hit
  its ceiling cannot place every timeline position; a slice of it inherits
  that, and the count and the degraded flag come along.
- **A window that starts inside a gap is refused, not moved.** There is no
  sample at that position to start from, and quietly starting at the next one
  would answer a different question than the one asked. The error names both
  edges of the gap so the caller can pick one deliberately. An *end* bound
  inside a gap is fine: it has an answer, which is the last sample before it.

Timestamps in a window keep the artifact's own origin, so the `time_s` of a
given sample is the same in a windowed export as in a full one.

For manual bounded-memory processing, iterate chunks via
`ppk2lab.capture.ArtifactReader.iter_raw_words()` or `iter_chunk_bytes()`,
both of which accept a `stored_start`/`stored_end` range. The CRC32 is always
checked over the whole chunk before any slicing: a per-chunk checksum is a
statement about the whole chunk, and checking part of one would be a weaker
claim wearing the same name.

## Derived views

Exports are reproducible views and never replace the artifact. Two shapes
exist and neither is ever the other's default: the raw exporters write one
record per stored sample whatever the capture's length, and the decimated
exporters write one record per timeline bucket only when a bucket width is
asked for. Their headers and record shapes share nothing, so a file cannot be
mistaken for the other kind — see `docs/decimation.md`.

## Relationship to the official `.ppk2` format

The official app's `.ppk2` stores processed samples (float32 µA plus an
aggregated digital state), not the device's raw 4-byte words; range,
counter, and exact raw values are not recoverable from it. An import/export
compatibility layer is planned post-`0.2.0` (`ROADMAP.md`); `.ppk2a` remains
the canonical evidence format.
