# Decimated export views

A PPK2 records at a fixed 100 kS/s. One hour is 360 million samples, about
19 GB of raw CSV; eight hours is a file most tools will not open. A decimated
export answers the questions that survive summarising — how much charge, what
the envelope looked like, where the peaks were — from a file small enough to
plot.

Decimation is **an export view of a 100 kS/s recording, never a capture
setting**. The acquisition rate is not configurable, the artifact always holds
every sample the instrument delivered, and nothing here ever rewrites it.

## Three rules this view is built on

1. **Opt-in, never automatic.** `export --format csv` emits raw per-sample
   rows whatever the capture length. Decimation happens only when
   `--decimate`/`--bucket-ms` is passed. An export whose *shape* depended on
   its input size would be derived data quietly taking the place of raw data.
2. **A different header.** The decimated files share no column name and no
   record shape with the raw export, so a file cannot be mistaken for the
   other kind — not by a human, not by a script that sniffs the first line.
3. **A bucket says what it does not contain.** Every bucket carries how many
   of its positions held a usable sample, how many held nothing at all, and
   how many held a sample that could not be used. A bucket sitting over a gap
   can never pass for a full one.

## The bucket record

A bucket covers a fixed span of **timeline** positions, so a gap consumes
bucket space instead of shifting later buckets earlier. Bucket `b` covers
`[origin + b·N, origin + (b+1)·N)` where `N` is the bucket width in samples
and `origin` is the capture's first timeline position. The last bucket is
truncated at the end of the capture rather than padded, so its unreached
positions are never reported as loss.

| Field | Type | Meaning |
|---|---|---|
| `bucket_start_index` | int | Timeline index of the bucket's first position |
| `t_start_s` | float | Seconds from the capture's time origin |
| `bucket_samples` | int | Timeline positions this bucket covers |
| `samples_in_bucket` | int | Positions holding a usable sample |
| `missing_in_bucket` | int | Positions holding no stored sample at all |
| `unknown_gaps_in_bucket` | int | Gaps of undetermined size starting in this bucket |
| `excluded_in_bucket` | int | Stored samples that could not be used |
| `saturated_in_bucket` | int | Usable samples pinned at the ADC's full-scale code |
| `complete` | bool | All three loss counts are zero |
| `mean_ua` | float \| null | Mean over the usable samples |
| `min_ua` | float \| null | Smallest usable sample |
| `max_ua` | float \| null | Largest usable sample |
| `charge_uc` | float \| null | Charge carried by the usable samples |
| `range_occupancy` | int[5] | Usable samples per auto-switching shunt range |
| `range_switches` | int | Range changes observed inside this bucket |

Counting invariant, exact for every bucket:

```text
samples_in_bucket + missing_in_bucket + excluded_in_bucket == bucket_samples
sum(range_occupancy) == samples_in_bucket
```

`missing_in_bucket` is data the instrument never delivered — a recorded gap.
`excluded_in_bucket` is data that arrived and cannot be trusted: an invalid
range field, a sample with no calibration constant for its range, or a value
beyond what the hardware can carry. They are counted apart because they are
different failures with different remedies, and folding either into
`samples_in_bucket` would dilute the mean with samples that carry no charge.

`unknown_gaps_in_bucket` is the third kind, and it is why `complete` is not
simply `missing_in_bucket == 0`. A gap whose size could not be determined —
a framing desync, a host stall — occupies no timeline width, so it consumes
no bucket positions and appears in none of the counts above. Data was still
lost there. A bucket carrying one is never `complete`, and the invariant
below holds as stated only because such a gap has no width to account for.

`min_ua`, `max_ua`, `mean_ua` and `charge_uc` are `null` — never `0` — when
`samples_in_bucket` is zero. Zero would claim the DUT drew nothing during a
span where nothing was measured.

### Why min and max, and not just a mean

A per-bucket mean is a low-pass filter. Decimating a duty-cycled load by 1000
turns a 12 mA transmit burst into a 60 µA-looking bucket, and the peak the
DUT actually drew disappears from every plot made from that file. `min_ua`
and `max_ua` are the bucket's envelope: they are exact — they are real
samples, not interpolations — and they survive further aggregation, so a
decimated view can be summarised again without ever losing the extremes.

`saturated_in_bucket` is what stops `max_ua` from lying in the other
direction. A sample sitting on the ADC's full-scale code is a ceiling, not a
measurement, so a bucket whose maximum is pinned reports a peak that is
itself a lower bound. Without this counter a decimated file would render a
clipped burst as a clean flat top — the classic way a measurement lies.

### The mean is defined by the charge, not the other way round

```text
mean_ua ≡ charge_uc / (samples_in_bucket × 10 µs)
```

over the **present** samples only. This is a definition, not an observation,
and it is what keeps re-decimation honest: charge and sample counts are
additive, so merging buckets and re-deriving the mean from the merged totals
gives the mean over the merged span — no matter how many times the series has
already been summarised. A mean stored as an independent statistic would have
to be re-averaged instead, and a mean of means weighted by nothing is wrong
the moment two buckets hold different sample counts, which is exactly what
happens wherever a gap falls.

The one residual is float64 summation order: merging three buckets and
summing one span of the same samples can differ in the last ULP, because the
additions happen in a different order. That difference does not grow with
further passes, and it is roughly ten orders of magnitude below the
instrument's own accuracy.

Every field aggregates without loss when consecutive buckets are merged:

| Field | Merge rule |
|---|---|
| `bucket_start_index`, `t_start_s` | Take the first bucket's |
| `bucket_samples`, `samples_in_bucket` | Sum |
| `missing_in_bucket`, `unknown_gaps_in_bucket` | Sum |
| `excluded_in_bucket` | Sum |
| `saturated_in_bucket`, `range_switches` | Sum |
| `range_occupancy` | Element-wise sum |
| `charge_uc` | Sum (`null` treated as absent, not as zero) |
| `mean_ua` | Re-derive from the summed charge and sample count |
| `min_ua`, `max_ua` | Min / max |
| `complete` | Logical AND |

`range_switches` counts a change between two *adjacent usable samples* and
attributes it to the bucket holding the later of the two, including when that
pair straddles a bucket boundary. That attribution is what makes the sum over
buckets equal the whole-window figure `measure` reports. A range difference
across a gap is never a switch: the two samples were not adjacent.

## Output formats

Both formats carry exactly the record above. Neither carries per-sample data,
and neither is a substitute for the artifact.

### CSV

```text
bucket_start_index,t_start_s,bucket_samples,samples_in_bucket,missing_in_bucket,
unknown_gaps_in_bucket,excluded_in_bucket,saturated_in_bucket,complete,mean_ua,
min_ua,max_ua,charge_uc,range0,range1,range2,range3,range4,range_switches
```

(one physical line; wrapped here to fit the page). `complete` is `1`/`0`,
matching the raw export's `valid` column. Empty cells mean `null`.

The raw export's header is
`timeline_index,time_s,current_ua,…` — no column name is shared with the
decimated header, and the two files can be told apart from their first line
alone.

Numbers are written at full float64 round-trip precision rather than rounded
to a fixed number of decimals, so the `mean_ua ≡ charge_uc / (n × 10 µs)`
identity can be checked from the file itself. The raw export rounds because
it is meant to be read; this one is meant to be recombined.

### JSON Lines

One object per bucket, each nested under a `bucket` key:

```json
{"bucket": {"bucket_start_index": 0, "t_start_s": 0.0, "bucket_samples": 1000,
 "samples_in_bucket": 1000, "missing_in_bucket": 0, "unknown_gaps_in_bucket": 0,
 "excluded_in_bucket": 0, "saturated_in_bucket": 0, "complete": true,
 "mean_ua": 100.0, "min_ua": 99.8, "max_ua": 100.2, "charge_uc": 1.0,
 "range_occupancy": [0, 0, 1000, 0, 0], "range_switches": 0}}
```

The nesting mirrors the raw JSONL export's `{"gap": …}` records: a consumer
that reads a line and looks at its single top-level key always knows what it
is holding. Raw sample records are flat objects with an `index` key, so no
line of one file can parse as a line of the other.

## Gap reasons live in the artifact

The decimated export records *how much* was missing in each bucket, not
*why*. Reasons (`counter_skip`, `host_overflow`, `stream_desync`, …) stay in
the manifest, where `ppk2lab inspect` reads them without materialising a
single sample. This is the general rule for every derived export: the
artifact is the evidence, and an export that tried to carry all of it would
just be a slower copy of it.

## Choosing a bucket width

`--decimate N` sets the width in samples; `--bucket-ms M` sets it in
milliseconds and is converted at the capture's own recorded sample rate
(`M × 100` samples at 100 kS/s). Both must resolve to at least one sample.

A useful starting point is one bucket per output pixel or per row you intend
to read. Two things worth knowing before choosing:

- **The envelope is preserved at any width; the shape between min and max is
  not.** A 1 s bucket over a device that wakes for 3 ms every second reports
  the sleep current as `min_ua`, the burst as `max_ua`, and a `mean_ua` the
  DUT never draws for a single sample. All three are true; only the last one
  is easy to misread.
- **Charge is exact at any width.** Summing `charge_uc` over every bucket
  reproduces the window's charge, because the buckets partition the window
  and every usable sample lands in exactly one of them.

## Estimating an export before writing it

`export --json` reports `estimated_bytes` and `bytes_per_record` for the
export it is about to perform, measured on a prefix of this capture's own
records rather than from a compiled-in constant — record width depends on the
values, so a table of averages would be a guess presented as a fact. The
figure is an estimate and is labelled as one.

As an order of magnitude on the author's machine: raw CSV is around 53 bytes
per sample and raw JSONL around 111, so one hour of capture is roughly 19 GB
and 40 GB respectively. A decimated record is larger than a raw one — it
carries nineteen fields instead of fifteen — but there are `N` times fewer of
them.

## What decimation does not do

- It does not change what was captured, and it cannot be requested at capture
  time. There is no such thing as a decimated `.ppk2a`.
- It does not make an over-long window honest: a bucket past the end of the
  capture reports its positions as missing, exactly as `measure` does.
- It does not filter. `min_ua`/`max_ua` include range-switch transients; pass
  `--filtered` to the raw export if you want the smoothed series, and read
  `docs/bandwidth.md` for the band over which any of these figures is valid.
- **It shrinks the output, not the read.** A bucket is computed from the
  samples it summarizes, so a decimated export without `--window` still
  materializes the whole capture and `--max-samples` applies exactly as it
  does to a raw one — an hour-scale artifact still fails with
  `CAPTURE_TOO_LARGE`. Pair `--decimate` with `--window START:END`, which
  reads only the chunks the window falls in, or raise the ceiling with
  `--max-samples none` and pay for it in RAM.
