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
  frame/word/transaction is closed with a `gap` error and confidence 0.
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
  `truncated`, `partial_word`, `reasserted`); an annotation with `gap` or
  `truncated` has confidence 0 and must never be treated as decoded data.
- `confidence` starts from the rate tier and is further reduced by measured
  signal quality (SPI: worst observed clock cycle in samples).

## UART decoder

Configuration: `rx` (D0-D7), `baud`, `data_bits` 5-9, `parity`
none/even/odd, `stop_bits` 1-2, `msb_first`, `invert`.

Behavior:

- **Center sampling with fractional phase**: bit k is sampled at
  `start_edge + (k + 0.5) × samples_per_bit` (float), so non-integer ratios
  (10.417 at 9600 baud) never accumulate drift.
- **Idle arming**: start-edge detection arms only after ≥ 1.5 bit times of
  continuous idle — at stream start, after a gap, and after a break. This
  prevents the decoder from locking onto the middle of a frame that a gap
  cut open and fabricating bytes. Consequence (documented limitation):
  frames that follow a gap back-to-back, with less than 1.5 bit of idle,
  are not decoded; decoding resumes at the next idle period.
- Frames report parity and framing errors; an all-zero frame extends into a
  `break` annotation that ends when the line returns high.
- Kinds emitted: `frame`, `break`, `error` (with `reason`:
  `incomplete_frame`).

`ppk2lab.decoders.uart.uart_bytes(annotations)` reconstructs the clean byte
stream (error frames excluded) with per-byte end indexes, for content
triggers and assertions.

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

## Extending

Decoders register in `ppk2lab.decoders.registry`; `ppk2lab capabilities
--json` generates the decoder surface from that registry so documentation
cannot drift. Planned post-0.1.0 decoders (low-speed bit-banged I2C, PWM,
Manchester/NRZ, GPIO markers, 1-Wire) must implement the same streaming
contract and rate-tier validation. CAN, USB, SWD, JTAG, I2S, and general
high-speed SPI are out of scope for this hardware.
