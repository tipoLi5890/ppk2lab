# Logic Port: D0-D7 wiring, VCC, bandwidth, and safety

Based on Nordic's official Logic Port documentation and hardware files
(`docs/sources.md`). Verify against the printed markings and the keyed
ribbon cable on your unit before wiring.

## Pinout (10-pin Logic Port, per official schematic)

| Pin | Signal |
|---|---|
| 1 | GND |
| 2-9 | D0-D7 |
| 10 | VCC (logic reference input) |

## Hardware safety rules

- **Logic VCC must be connected to the DUT's VCC** (official range
  1.65-5.5 V). The port uses an FXMA108 level shifter between the DUT's
  logic level and the board's 3.3 V domain; without a valid VCC reference
  the inputs do not read correctly.
- Share ground between the PPK2 and the DUT.
- Do not exceed the DUT's own logic voltage on any D pin, and never wire a
  D pin to a supply rail.
- If the DUT is powered by the PPK2 (source mode), remember that captures
  never enable DUT power by themselves — configure power explicitly first.

## Sampling model and bandwidth

Digital inputs are sampled at the same fixed 100 kS/s as current (one
snapshot per 10 us; typical bandwidth ~50 kHz). Multiple transitions inside
one sample period are unrecoverable. Consequences:

- pulses shorter than ~10 us may be missed entirely;
- edge timestamps quantize to the 10 us grid;
- protocol decoding is limited to the rate tiers in `docs/decoders.md`.

## Analysis primitives

`ppk2lab.logic` is an internal module, with two exceptions:
`docs/api-baseline.md` lists `edges` and `pulses` in section 4b, so those two
are frozen with the rest of the public surface. `iter_transitions` is not
listed and may change without a `SCHEMA_VERSION` bump. The
`export --format vcd` output is a frozen file format and does not move.

- `iter_transitions(events)` — state changes; the first sample (and the
  first after any gap) re-states levels with `after_gap=True` instead of
  claiming an edge.
- `edges(events, channel)` — observed edges only; nothing is claimed across
  gaps.
- `pulses(events, channel)` — constant-level runs bounded by observed edges;
  runs adjacent to capture ends or gaps are excluded because their width
  would be a false measurement.
- `ppk2lab export --format vcd` — PulseView/GTKWave-compatible VCD at a
  10 us timescale; all exported channels are driven to `x` during gaps so
  missing data can't be mistaken for a constant level.

## Diagnosing wiring

Suggested checks (the `ppk2lab-operate` skill routes to the same ones in
`references/diagnostics.md`). They are reasoned from the sampling model and
exercised in tests against generated waveforms: this project has not applied
a known digital signal to D0-D7 on hardware, because the hardware session
behind the rest of these docs had no MCU fixture attached.

1. `ppk2lab capture --duration 1s` and export VCD; a floating channel shows
   noise or a constant level regardless of DUT activity.
2. Walking-one / walking-zero patterns from the DUT verify each pin mapping.
3. If all channels read constant despite activity, check Logic VCC first.
