# Decoder contracts: UART, SPI, confidence, gaps, physical limits

## The physical budget

PPK2 samples D0-D7 at a fixed 100 kS/s (typical analog bandwidth ~50 kHz).
Decoding is therefore a low-speed capability, not a MHz-class logic
analyzer. Support tiers are enforced at decoder construction:

| samples per bit/cycle | tier | behavior |
|---|---|---|
| ≥ 10 | validated | full support, confidence 1.0 |
| ≥ 5 | conditional | decodes; warning attached; confidence 0.7 |
| ≥ 2.5 | experimental | refused unless `allow_experimental`; confidence 0.4 |
| < 2.5 | unsupported | always refused (`DECODER_RATE_UNSUPPORTED`) |

For UART that means 9600 baud validated, 19200 conditional, 38400
experimental, ≥ 57600 refused. For SPI: ≤ 10 kHz validated, ≤ 20 kHz
conditional, ≤ 40 kHz experimental, above refused.

## Streaming contract (all decoders)

Decoders are stateful stream consumers (`ppk2lab.decoders.StreamingDecoder`):

- `feed(LogicChunk(start_index, logic_bytes))` — contiguous timeline data;
  an unannounced forward jump is converted into an implicit gap
  notification; overlapping data is an error.
- `notify_gap(GapEvent)` — must be called for every loss; any in-progress
  frame/word/transaction is closed with a `gap` error and confidence 0. UART
  additionally emits one `error` annotation per gap (`reason: "sample_gap"`,
  spanning the missing samples, zero-width when the gap's size is unknown)
  even when no frame was in flight, so the loss is visible in the annotation
  stream and downstream consumers can see that the bytes either side of it
  were not sent back to back. A gappy capture therefore decodes to one more
  annotation per gap than it used to, and `measure --group-by kind` reports
  them under `error`.
- `flush()` — end of stream; pending state is reported as `truncated`.
- `reset()` — full state reset for reuse.

Chunk boundaries never change results: tests feed the same waveforms at
chunk sizes from 1 sample upward and require identical annotations.

## Annotations

```json
{
  "decoder": "uart", "kind": "frame",
  "start_sample": 1500, "end_sample": 1604,
  "fields": {"value": 84, "byte": "0x54", "char": "T", "channel": "D0"},
  "confidence": 1.0,
  "errors": []
}
```

- `start_sample`/`end_sample` are exact timeline indexes into the capture —
  every conclusion points back to raw samples.
- `errors` is a list of stable strings (`parity`, `framing`, `gap`,
  `truncated`, `unsynced`, `partial_word`, `reasserted`).
- **A non-empty `errors` list always means `confidence == 0.0`.** The
  annotation is kept as evidence of what the line did; it is never decoded
  data. Filtering on `errors` and filtering on `confidence` give the same
  answer, so a consumer may use either.
- `confidence` starts from the rate tier and is further reduced by measured
  signal quality (SPI: worst observed clock cycle in samples).

## UART decoder

Configuration: `rx` (D0-D7), `baud`, `data_bits` 5-9, `parity`
none/even/odd, `stop_bits` 1-2, `msb_first`, `invert`.

Behavior:

- **Center sampling with fractional phase**: bit k is sampled at
  `start_edge + (k + 0.5) × samples_per_bit` (float), so non-integer ratios
  (10.417 at 9600 baud) never accumulate drift.
- **Confirmed idle before alignment is claimed**: a receiver cannot know
  where a frame starts until it has seen a high run longer than any that can
  occur inside one. Every bit of a frame except the start bit can be high at
  the same time (all-ones data, a high parity bit, the stop bits), so a run
  of `frame_bits - 1` bit times — 9 bit times at 8N1 — can occur with no
  idle at all. The decoder therefore requires **one whole frame time** of
  continuous idle (104 samples at 9600 8N1, `frame_bits × samples_per_bit`)
  before it treats a falling edge as provably a start bit. The extra bit
  time over the theoretical minimum covers sample-grid quantization and a
  run that resumes mid-bit after a gap.
- **`unsynced`**: this state is entered at stream start, after every gap,
  and after every break. Until confirmed idle arrives, candidate frames are
  still emitted — the raw evidence is never discarded — but tagged
  `errors: ["unsynced"]` with confidence 0, and they never join the byte
  stream, anchor an assertion, or fire a content trigger. The decoder still
  arms provisionally after 1.5 bit times so those candidates exist at all.
- Consequence (documented limitation): a line that never idles for a full
  frame time produces no clean bytes after a gap, and none at the start of a
  capture, until real idle occurs. `ppk2lab capture --trigger` starts
  mid-stream on every run and hits this with no gap involved. There is
  deliberately **no option to lower the threshold** — a shorter requirement
  is a supported way to fabricate bytes.
- Frames report parity and framing errors; an all-zero frame extends into a
  `break` annotation that ends when the line returns high.
- Kinds emitted: `frame`, `break`, `error` (with `reason`:
  `incomplete_frame` or `sample_gap`).

`ppk2lab.decoders.uart.uart_runs(annotations)` returns the clean byte stream
split into runs of frames that were observed **back to back**: a gap, an
unsynchronized frame, a parity or framing error, or a break starts a new
run. `uart_bytes(annotations)` is the concatenation of those runs, with
per-byte end indexes, for callers that only want the payload. Anything that
must not match across a discontinuity — assertion anchors, content triggers
— works from the runs instead, because a pattern spanning two runs is a byte
sequence that never appeared on the wire.

## SPI decoder

Configuration: `sclk` (required), `mosi`/`miso` (at least one), optional
`cs` with `cs_active_low`, `mode` 0-3 (CPOL/CPHA), `word_bits` 4-32,
`msb_first`, optional `expected_clock_hz` (enables tier validation),
`idle_timeout_samples` (transaction grouping without CS).

Behavior:

- Edge-driven: bits latch on the mode's sampling edge (leading edge for
  CPHA=0, trailing for CPHA=1).
- Transactions group words by CS assertion, or by clock-idle timeout when no
  CS channel is configured. CS deassert mid-word emits the partial word with
  a `partial_word` error.
- The first sample after start/gap only establishes line levels — no edge is
  ever claimed across missing data.
- Per-word confidence reflects the worst observed clock cycle: on the
  100 kS/s grid a 20 kHz clock quantizes to alternating 6/4-sample cycles,
  and the 4-sample worst case honestly lowers confidence below the nominal
  conditional tier.
- Kinds emitted: `word`, `transaction`.
- A word pattern is only recognized **inside one transaction**. Two words
  exchanged under separate CS assertions were two exchanges, whatever they
  spell when concatenated: matching `spi(0x9f, 0x00)` with the opcode in one
  transaction and the response in another proves nothing about the device.
  `spi(...)` assertion anchors and SPI content triggers both enforce this.

## Anchoring on decoded content

`assert --rule 'after uart("TX_DONE"), ...'` and `capture --trigger 'uart D0
9600 "BOOT"'` only fire on evidence that is contiguous:

- the bytes must come from one `uart_runs` run — a gap, an unsynchronized
  frame, or a frame error between them means they were not sent back to
  back, so they are not the event;
- SPI words must come from one transaction.

When a pattern *would* have matched across such a boundary, `assert` reports
`no_event` (exit 1) and attaches a warning naming how many occurrences were
rejected and why, so the difference from a plain "not present" is visible.

## Extending

Decoders register in `ppk2lab.decoders.registry`; `ppk2lab capabilities
--json` generates the decoder surface from that registry — including each
decoder's `gap_policy` and `sync_policy` — so documentation cannot drift.
Planned post-0.2.0 decoders (low-speed bit-banged I2C, PWM, Manchester/NRZ,
GPIO markers, 1-Wire) must implement the same streaming contract and
rate-tier validation. CAN, USB, SWD, JTAG, I2S, and general
high-speed SPI are out of scope for this hardware.
