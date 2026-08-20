# Triggered capture (measurement-only)

Record exactly the interesting window: `[fire - pre, fire + post)`.

## Trigger specs

```text
current>10mA          current<5uA        (+ --trigger-hold N samples)
digital D3 rising     digital D3 falling
digital mask=0x0f value=0x05
uart D0 9600 "BOOT"
spi 0x9f,0x00         (channels via --spi-sclk/--spi-mosi/...)
```

Translate intent: "when it wakes" → `current>1mA` with a hold; "after the
boot banner" → `uart D0 9600 "BOOT"`.

## Run

```bash
ppk2lab capture --device S --trigger 'uart D0 9600 "BOOT"' \
  --pre 100ms --post 1s --trigger-timeout 30s --output boot.ppk2a --json
```

Always set `--trigger-timeout` for unattended runs. Verify in the result:
`trigger.fired`, `trigger.fire_index`, `complete`; the manifest records the
trigger description for reproducibility.

## Failure semantics (relay honestly)

- Timeout without firing → exit 6, `interruption.reason: trigger_timeout`;
  suggest loosening the condition or checking wiring. A stream that simply
  ends first gives `trigger_never_fired`.
- Less than `pre` existed before the fire → window starts at the first
  available sample; manifest `start_index` shows the truth.
- Gaps reset hold counts and content-decoder state — a trigger cannot fire
  on missing data, and gaps are routine: 5 in a 60 s capture on an idle
  host (capture.md). A long `--trigger-hold` on a busy host can therefore
  be reset repeatedly before it ever completes. UART/SPI content triggers
  inherit the decoder rate tiers (see `ppk2lab capabilities --json`),
  including `--allow-experimental` gating.
- **A content trigger only fires on evidence that was actually on the
  wire.** A UART pattern cannot fire before the decoder has seen a
  confirmed idle run — at stream start, after a gap, and after a break, the
  frames are `unsynced` and never become bytes (decode.md). A pattern is
  also not matched around a corrupted frame or across two SPI CS
  transactions: the match window is cleared at every such discontinuity.
  So a trigger that used to fire on `D, O, <corrupted>, N, E` no longer
  does. If a trigger stops firing after an upgrade, the pattern was being
  matched on stitched evidence — fix the signal, don't loosen the trigger.
- Because the confirmed-idle rule applies at stream start too, expect a
  UART content trigger to ignore whatever was mid-transmission when the
  capture began. Give the DUT a quiet moment before the event you want, or
  trigger on something else and search the decode afterwards.

A content trigger runs the same decoders as `decode`, and those have never
been fed a real signal — there is no MCU fixture, so no content trigger has
a measured hardware result (decode.md). A trigger that does not fire is
therefore ambiguous between "the DUT did not send it" and "the decode of
this signal is not what was assumed"; capture untriggered and decode
offline to tell them apart.

Done when: the result reports `trigger.fired` and, if it fired,
`fire_index` and the window actually recorded — and a trigger that did not
fire is reported as "not observed", never as "did not happen".
