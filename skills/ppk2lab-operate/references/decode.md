# Decoding: UART, SPI, and D0-D7 logic (offline, read-only)

## Physical budget — state it up front

D0-D7 sample at a fixed 100 kS/s (~50 kHz bandwidth): pulses < ~10 us can
be missed, edges quantize to 10 us. This is a low-speed analyzer. Rate
tiers are enforced by the tool; get current numbers from
`ppk2lab capabilities --json` (`decoders[].rate_tiers`). Summary: UART
≤ 9600 validated / 19200 conditional / 38400 experimental (needs
`--allow-experimental`) / ≥ 57600 refused; SPI ≤ 10 kHz validated /
≤ 20 kHz conditional / ≤ 40 kHz experimental / above refused. Refuse
politely beyond that — suggest a MHz-class analyzer.

## UART

```bash
ppk2lab decode run.ppk2a --uart D0 --baud 9600 --output uart.jsonl --json
# options: --data-bits 5..9  --parity even|odd  --stop-bits 2  --invert  --msb-first
```

- Reconstruct content from clean frames only (empty `errors`); frames with
  `parity`/`framing`/`gap`/`truncated` errors are corruption evidence, not
  data.
- After a gap the decoder re-syncs only after ≥ 1.5 bit times of idle;
  back-to-back frames right after a gap are deliberately not decoded —
  report them as unrecoverable due to data loss.
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
