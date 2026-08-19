# Software triggers

Triggers run on the host over the live 100 kS/s stream and gate what a
capture stores: the window `[fire - pre, fire + post)` with full gap
fidelity. They change nothing on the device — triggered capture is still
measurement-only.

## Detector types

| Spec (CLI) | Fires when |
|---|---|
| `current>10mA`, `current<5uA` | calibrated current crosses the threshold, optionally held for `--trigger-hold N` samples |
| `digital D3 rising` / `falling` | an observed edge on the channel |
| `digital mask=0x0f value=0x05` | `(logic & mask) == value`, optionally held |
| `uart D0 9600 "BOOT"` | the clean UART byte stream ends with the pattern (fires at the last byte's end sample) |
| `spi 0x9f,0x00` | consecutive clean SPI words match (channels from `--spi-*` options) |

Python: `CurrentThresholdTrigger`, `DigitalEdgeTrigger`,
`DigitalPatternTrigger`, `UartContentTrigger`, `SpiContentTrigger`, composed
with `TriggerEngine(detector, pre_samples=..., post_samples=...)`.

## Gap semantics

Missing data never counts as evidence:

- threshold/pattern hold counts reset at every gap;
- edge detectors forget their previous level across a gap (no edge is
  claimed between the last pre-gap and first post-gap sample);
- content triggers reset their decoder and match window on gaps;
- gaps overlapping the fired window are preserved in the stored capture.

## Pre/post windows

The engine keeps a ring buffer of at least `pre` samples. On fire it emits
the buffered window trimmed to `[fire - pre, fire + post)` and passes the
live stream through until `post` samples after the fire index. If the
capture began less than `pre` samples before the fire, the window starts at
the first available sample (the manifest records the true `start_index`).

## Timeouts and honesty

`--trigger-timeout DUR` aborts a capture whose trigger never fires; the
result is `complete: false` with `interruption.reason = "trigger_timeout"`
(exit 6). A stream that ends before firing reports
`trigger_never_fired`. There is no code path that fabricates a trigger
window from partial conditions.

## Rate limits for content triggers

UART/SPI content triggers embed real decoders and inherit the rate tiers in
`docs/decoders.md`, including `--allow-experimental` gating.
