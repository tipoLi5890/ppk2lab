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
  suggest loosening the condition or checking wiring.
- Less than `pre` existed before the fire → window starts at the first
  available sample; manifest `start_index` shows the truth.
- Gaps reset hold counts and content-decoder state — a trigger cannot fire
  on missing data. UART/SPI content triggers inherit the decoder rate
  tiers (see `ppk2lab capabilities --json`), including
  `--allow-experimental` gating.
