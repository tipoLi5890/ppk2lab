# Decoding: UART, SPI, and D0-D7 logic (offline, read-only)

## Physical budget — state it up front

D0-D7 sample at a fixed 100 kS/s (~50 kHz bandwidth): pulses < ~10 us can
be missed, edges quantize to 10 us. This is a low-speed analyzer. Rate
tiers are enforced by the tool; read the current limits from
`ppk2lab capabilities --json` (`decoders[].rate_tiers`, which carries its
own `note`) rather than from memory. The tiers are ratios of samples to bit
or clock period at that fixed rate, so beyond the top tier refuse politely
and suggest a MHz-class analyzer.

**"Validated" is a support tier, not a measured error rate.** No decoder in
this project has been run against a real signal: there is no MCU fixture,
so no UART or SPI error rate has been measured on hardware, and the tiers
are a sampling-ratio policy rather than a result. Say that when a decode is
being offered as evidence.

The same entry publishes `sync_policy` and `gap_policy` per decoder — the
two rules that decide what counts as data. Read both before explaining a
decoded result. `decode` has no
`--window` — a decoder carries sync state across block boundaries — so a
long artifact needs `--max-samples` (see capture.md).

## UART

```bash
ppk2lab decode run.ppk2a --uart D0 --baud 9600 --output uart.jsonl --json
# options: --data-bits 5..9  --parity even|odd  --stop-bits 2  --invert  --msb-first
```

- Reconstruct content from clean frames only (empty `errors`). A non-empty
  `errors` list always means `confidence: 0.0`, for both decoders — filter
  on `errors` and the confidences follow.
- `unsynced` is the error tag to understand. A receiver cannot know where a
  frame starts until it has seen a high run longer than any that can occur
  inside one — at 8N1 that is a whole frame time. Until that idle run is
  observed (at stream start, after every gap, after every break) candidate
  frames are still emitted as raw evidence of what the line did, tagged
  `errors: ["unsynced"]` with confidence 0. They are never bytes: they do
  not join the byte stream, anchor an assertion, or fire a trigger. There is
  deliberately no option to lower the threshold. Report them as "the line
  was active here, but where the frames start is unknown", not as data.
- `parity`/`framing`/`gap`/`truncated` errors are corruption evidence, not
  data, on the same footing.
- **Every gap now leaves its own annotation**, even between frames with none
  in flight: `kind: "error"`, `errors: ["gap"]`, `fields.reason:
  "sample_gap"`, spanning the missing samples. Before this a gap that fell
  between two frames was invisible in decoded output. Expect one extra
  `error` annotation per gap, and expect `measure --group-by kind` to list
  them under `error`. Budget for several: a 60 s capture on an idle host
  measured 5 gaps (capture.md), and each one costs a resync as well as an
  annotation — after a gap the decoder is `unsynced` again until it sees a
  whole frame time of idle.
- Searching for a byte pattern: use `ppk2lab.decoders.uart.uart_runs()`,
  which splits the clean byte stream at every discontinuity and returns
  `(data, end_samples)` per run. `uart_bytes()` is its concatenation. A
  match spanning two runs is a sequence that was never on the wire — search
  each run on its own.
- Many errors at a plausible baud → wrong baud/channel, inverted signal, or
  missing Logic VCC (see diagnostics.md).

## SPI

```bash
ppk2lab decode run.ppk2a --spi-sclk D1 --spi-mosi D2 --spi-miso D3 \
  --spi-cs D4 --spi-mode 0 --spi-clock-hz 10000 --output spi.jsonl --json
# options: --spi-word-bits 4..32  --spi-lsb-first  --spi-cs-active-high
```

- Pass `--spi-clock-hz` when known so tier validation runs; per-word
  confidence also reflects the worst observed clock cycle.
- Without CS, transactions group by clock-idle timeout.
- Wrong mode shows as consistently shifted bits — try the other CPHA
  first (0↔1 or 2↔3). All-zero MISO with sensible MOSI usually means MISO
  unwired or a silent DUT, not a decoder fault.
- `partial_word` / `reasserted` / `gap` / `truncated` mark unreliable data.
- A word pattern is only ever recognized **inside one transaction**. Words
  exchanged under two CS assertions were two exchanges, whatever they spell
  when concatenated — so a `spi(0x9f, 0x00)` pattern that spans a CS
  boundary is not evidence of one exchange, and neither `assert` nor a
  content trigger will treat it as one.

## Logic timing and VCD

```bash
ppk2lab export run.ppk2a --format vcd --output run.vcd --channels D0-D7
```

`x` states in the VCD are sample gaps — missing data, not glitches. For
programmatic timing use Python: `ppk2lab.logic.edges` / `pulses` over
`Capture.load(...).iter_events()`; pulses touching gaps or capture ends are
excluded because their width would be a false measurement. Channel-mapping
doubts: walking-one from the DUT, confirm each expected channel.

## Per-event energy

After decoding, measure the annotations:

```bash
ppk2lab measure run.ppk2a --annotations uart.jsonl --group-by frame --json
ppk2lab measure run.ppk2a --annotations spi.jsonl --group-by transaction --json
```

Always report: counts, error counts, confidence tier, annotations path, and
which regions were unrecoverable due to gaps.

Done when: every reported byte or word came from an annotation with an
empty `errors` list, the unsynced and errored regions are described as
line activity rather than data, and the report says the decoders carry no
measured error rate from real hardware.
