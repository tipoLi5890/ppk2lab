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
  (100000), digital channels, the requested duration/limit, and the trigger
  description for triggered captures;
- `timeline`: `sample_period_ns` (10000), `start_index` (nonzero for
  triggered captures whose window begins mid-stream), `degraded` (true once
  an unknown-size gap occurred — indexes after that point no longer map to
  wall-clock time);
- `samples`: encoding `u32le-v1`, `stored_count`, `invalid_range_count`,
  SHA-256 over the concatenated raw sample bytes, and a chunk table with
  per-chunk `first_stored_index`, `count`, and CRC32;
- `gaps`: every known loss (`index`, `missing`, `reason`, `ambiguous`) in
  timeline order;
- `complete`: true only when the requested capture finished without
  interruption **and** without gaps;
- `interruption`: `null` or `{reason, detail}` (`keyboard_interrupt`,
  `transport_error`, `trigger_timeout`, `trigger_never_fired`);
- `stats`: precomputed summary (window-stats schema) for quick inspection —
  always recomputable from the raw samples;
- `warnings`: human-readable notes mirrored from the capture run.

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
  chunk byte counts, per-chunk CRC32, and the manifest SHA-256 against the
  stored data before returning samples.

## Reading

```python
import ppk2lab
cap = ppk2lab.Capture.load("run.ppk2a")     # verified, in-memory
for event in cap.iter_events():             # SampleBlock | GapEvent, timeline order
    ...
```

For bounded-memory processing of very large captures, iterate chunks via
`ppk2lab.capture.ArtifactReader.iter_raw_words()`.

## Relationship to the official `.ppk2` format

The official app's `.ppk2` stores processed samples (float32 µA plus an
aggregated digital state), not the device's raw 4-byte words; range,
counter, and exact raw values are not recoverable from it. An import/export
compatibility layer is planned post-0.1.0 (`ROADMAP.md`); `.ppk2a` remains
the canonical evidence format.
