# Hardware diagnostics

Read-only by default; a short verification capture is measurement-only.
Never change voltage or DUT power "to test a theory" without asking.

## Symptom → first checks

| Symptom | First checks |
|---|---|
| All D0-D7 constant despite DUT activity | Logic VCC (pin 10) connected to DUT VCC (1.65-5.5 V)? Shared GND? |
| One channel dead, others fine | Pinout: pin 1 GND, pins 2-9 = D0-D7; walking-one from the DUT |
| Random digital noise | Floating unconnected inputs — expected, not a fault |
| Current ~0 in source mode | Was DUT power actually applied? (power state has no readback — ask what was configured) |
| Current pinned at range max | Load beyond the 1 A path or a short — stop and inspect physically |
| Spikes at level changes | Range-switch transients; compare raw vs `--filtered` before blaming the DUT |
| Frequent sample gaps | Host load / USB path: close apps, avoid unpowered hubs; gaps are honest markers |

## Workflow

1. Reproduce cheaply:
   `ppk2lab capture --device S --duration 1s --output diag.ppk2a --json`
2. Digitals: `ppk2lab export diag.ppk2a --format vcd --output diag.vcd`;
   `x` regions are gaps.
3. Current: `ppk2lab measure diag.ppk2a --json` — check `invalid_range`,
   `not_convertible`, gap counts, physical plausibility of min/max.
4. Report: observed symptom → evidence (sample ranges) → most likely
   physical cause → suggested fix. Aim to narrow to at most two physical
   candidates, each with its supporting evidence window.
