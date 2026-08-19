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
| `uart D0 9600 "BOOT"` | consecutive clean UART frames spell the pattern (fires at the last byte's end sample) |
| `spi 0x9f,0x00` | consecutive clean SPI words inside one transaction match (channels from `--spi-*` options) |

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

## Content triggers fire only on contiguous evidence

A pattern is an event only if the bytes or words that spell it were adjacent
on the wire. Both content detectors therefore **clear the match window at any
annotation that is not a clean frame or word** — an errored or unsynchronized
frame, a break, a transaction boundary, a gap, or a UART value wider than a
byte on a 9-bit decoder. Without that, `uart D0 9600 "DONE"` fires on
`D, O, <corrupted frame>, N, E`, and `spi 0x9f,0x00` fires with the opcode in
one CS transaction and the response in the next. Neither is the event the
caller asked about.

The UART consequence is worth planning for: a UART content trigger cannot
fire before the decoder has seen one whole frame time of continuous idle,
because until then it does not know where a frame starts (`docs/decoders.md`,
"Confirmed idle before alignment is claimed"). `capture --trigger` joins a
line already running on every run, so a DUT that transmits back to back with
no idle produces no clean bytes and the trigger never fires. There is
deliberately no option to lower the threshold. The same requirement applies
again after every gap and every break.

## Timeouts and honesty

`--trigger-timeout DUR` aborts a capture whose trigger never fires; the
result is `complete: false` with `interruption.reason = "trigger_timeout"`
(exit 6). A stream that ends before firing reports
`trigger_never_fired`. There is no code path that fabricates a trigger
window from partial conditions.

A triggered capture also declines the wall-clock rate check
(`timeline.rate_check: "not_applicable"`). Pre-trigger samples are emitted at
the moment of the fire, so wall time covers only the post-trigger window
while the timeline covers both; the comparison could read 200 kS/s, or hide a
50% loss as healthy. Declining to answer is the honest outcome, and the gap
table still reports whatever the counter saw.

## Rate limits for content triggers

UART/SPI content triggers embed real decoders and inherit the rate tiers in
`docs/decoders.md`, including `--allow-experimental` gating.
