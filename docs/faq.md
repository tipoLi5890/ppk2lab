# FAQ

Answers to the questions the PPK2 ecosystem asks most often, including the
ones whose honest answer is "the hardware cannot do that".

## Can I change the sample rate?

No. The PPK2 samples at a fixed 100 kS/s and that is what it streams; there
is no command to change it. Tools that offer a "sampling rate" setting are
choosing how many of those samples to keep, which is a different thing.

If you want smaller files or coarser data, reduce after the fact rather than
pretending the acquisition changed: export a window, or (from `0.2.x`) use
decimation, which reports mean, min, and max per bucket. A bare mean would
hide exactly the current spikes you bought this instrument to see.

## Why do my numbers differ from another tool's?

Rather than guess at another tool's internals, here is what ppk2lab does, so
you can account for the difference:

1. **Filtering.** ppk2lab reports the raw calibrated series by default.
   Range switches produce short transients, and any tool that smooths them
   will report different peaks and a different integral. `--filtered` adds a
   smoothed series alongside the raw one; see `docs/calibration.md`.
2. **Integration window.** Averages depend on exactly which samples are
   included, so ppk2lab always reports the sample window it used.
3. **Missing samples.** ppk2lab averages only over samples that exist,
   reports `covered_fraction`, and marks charge as a lower bound when gaps
   overlap the window.
4. **Accuracy.** Nordic specifies ±10% per range (±15% on the top range).
   Two tools agreeing to three digits would be a coincidence, not a proof.

## Why is `energy_uj` null?

Because nothing measured the voltage. Energy is `charge x voltage`, and the
PPK2 measures only current.

- **Source Meter mode**: the DUT runs from VOUT, so the configured setpoint
  is a defensible supply voltage and energy is computed (still a setpoint —
  the DUT's terminal sees slightly less after shunt burden and lead drop).
- **Ampere Meter mode**: the DUT runs from its own supply, which the meter
  never sees. The device's VDD field is a leftover setpoint, so energy is
  reported as `null` rather than fabricated.

Pass the real supply voltage explicitly to get energy anyway:

```bash
ppk2lab measure run.ppk2a --assume-voltage-mv 3300 --json
```

The result records `voltage_basis: "caller_override"` and
`voltage_measured: false`, so the assumption travels with the number.

## What is the difference between Ampere and Source mode?

| | Ampere Meter | Source Meter |
|---|---|---|
| Who powers the DUT | its own supply | the PPK2's VOUT |
| What is measured | current through the meter | current the PPK2 supplies |
| Voltage setting | still feeds the calibration correction term | also sets what the DUT receives |
| Energy | needs `--assume-voltage-mv` | derived from the setpoint |

A common surprise: in Ampere mode the DUT will not run from VOUT. If it is
not powered separately, the capture reads near zero — which is why ppk2lab
warns when DUT power is unknown.

## My capture says `complete: false`. Is the data useless?

No — it is annotated. The samples that arrived are all there; the gaps are
listed with their timeline positions, so nothing is silently compressed.
What you must not do is treat means and integrals over a gappy window as if
they covered the whole window: `charge_is_lower_bound` and
`covered_fraction` exist to say so.

Exit code 6 means the same thing at the CLI boundary: not a failure, not a
success — data is missing and the caller decides.

## What is `timeline_compression`?

The device numbers its samples with a 6-bit counter (0-63), so it can only
describe losses smaller than 64 samples. A larger burst aliases, and a loss
of exactly 64, 128, ... samples is invisible to the counter entirely.

ppk2lab therefore also compares the sample timeline against the host's wall
clock. If a capture advanced far slower than 100 kS/s, samples were lost
however quiet the counter stayed, and the capture is marked incomplete with
`achieved_sample_rate_hz` and `rate_deficit_ratio` reported. Usual causes:
a loaded host, an unpowered USB hub, or a busy USB controller.

## Why does discovery show two ports?

Firmware 1.2.0 and newer expose a second (shell) CDC port. ppk2lab picks the
measurement port by USB interface number where the OS reports one, and
otherwise probes with the read-only metadata command. On macOS, where no
interface numbers are exposed at all, the probe is the normal path.

## `PORT_BUSY` / `PERMISSION_DENIED` — what now?

Follow the `remediation` field; it is platform-specific. On Windows and
macOS this nearly always means another application (usually the nRF Connect
Power Profiler) holds the port exclusively. On Linux it is usually group
membership: add your user to `dialout`/`uucp` or install the udev rule, then
replug. `ppk2lab doctor --json` diagnoses both.

## Can it decode I2C / faster UART / MHz SPI?

Not reliably, and it will say so rather than pretend. At 100 kS/s there are
about 10 samples per bit at 9,600 baud and roughly one at 115,200. The rate
tiers in `docs/decoders.md` are enforced in code: validated rates decode,
conditional rates warn, experimental rates need an explicit opt-in, and
anything beyond is refused. Additional low-speed decoders (bit-banged I2C,
PWM, 1-Wire) are on the roadmap; MHz-class protocols never will be.

## Does anything here power my DUT without asking?

No. Enabling DUT power, changing the source voltage, changing mode, and
resetting are the only state-changing operations, they never happen
implicitly, and `configure` is a dry run until `--apply`. You can also set a
hard ceiling for a fragile DUT:

```bash
ppk2lab configure --device S --max-voltage-mv 3600 --voltage-mv 3300 --apply
export PPK2LAB_MAX_VOLTAGE_MV=3600   # or session-wide
```

## How do I try it without hardware?

Add `--simulate` to any command. It runs a built-in simulated PPK2 with a
repeating activity cycle (current burst, UART traffic, an SPI transaction, a
square wave). It exercises the toolchain end to end — but simulated numbers
are not measurements, and results say `simulated: true`.
