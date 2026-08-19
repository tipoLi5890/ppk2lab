# CLI reference

Every command accepts the global flags `--json` (emit the envelope contract)
and `--simulate` (use the built-in simulated PPK2), before or after the
subcommand. `PPK2LAB_SIMULATE=1` is equivalent to `--simulate`. Exit codes
are documented in `docs/SPEC.md`. `ppk2lab capabilities --json` is the
machine-readable version of this page, generated from the live parser.

## discover — read-only

```bash
ppk2lab discover [--json]
```

Lists PPK2 devices with serial numbers and ports (roles: `measurement`,
`shell`, `unknown`). An empty list is not an error (warning + remediation).

## info — read-only

```bash
ppk2lab info [--device SERIAL | --port PATH] [--json]
```

Device identity, state (mode, VDD), parsed metadata/calibration, and any
calibration ranges that are missing.

## capabilities — read-only

Full machine-readable surface: device limits, commands with options and
state-changing classification, decoder tiers, exit codes, error catalog,
schema list.

## schema — read-only

```bash
ppk2lab schema --list
ppk2lab schema envelope        # prints the raw JSON Schema
```

## doctor — read-only by default

```bash
ppk2lab doctor [--device SERIAL] [--stream-check 1s] [--json]
```

Checks Python, pyserial, enumeration, device open, metadata, calibration.
`--stream-check` opts into a short measurement (start/stop only — never DUT
power) verifying the 100 kS/s rate and gap-free streaming.

## configure — state-changing (dry-run by default)

```bash
ppk2lab configure --device SERIAL [--mode ampere|source]
                  [--voltage-mv MV] [--max-voltage-mv MV]
                  [--dut-power on|off] [--apply]
```

`--max-voltage-mv` (or `PPK2LAB_MAX_VOLTAGE_MV`) sets a session ceiling below
the device limit, for a DUT that would be damaged above a known level; a
request above it is refused before any byte reaches the wire.

Without `--apply` nothing touches hardware; the result shows the projected
state. With `--apply`, each change reports requested/before/after and
readback status. Out-of-range voltage exits 8
(`VOLTAGE_OUT_OF_RANGE`) before any byte reaches the device.

## capture — measurement (never enables DUT power)

```bash
ppk2lab capture --device SERIAL (--duration 5s | --samples N | --trigger SPEC)
                [--output run.ppk2a] [--overwrite] [--digital D0-D7]
                [--pre 100ms] [--post 1s] [--trigger-timeout 30s]
                [--trigger-hold N] [--allow-experimental] [--spi-* ...]
                [--assume-voltage-mv MV] [--in-memory]
```

`--assume-voltage-mv` supplies the DUT supply voltage the meter cannot
measure, which is what makes energy computable for an Ampere-mode capture;
the result records it as an explicit assumption. Without `--output` the
capture is buffered in RAM and is refused beyond 60 s unless `--in-memory`
confirms that is intended.

The result also carries a `timeline` block cross-checking the sample
timeline against the wall clock; a capture that advanced far slower than
100 kS/s lost samples the 6-bit counter could not report and is marked
incomplete (exit 6).

Trigger specs: `current>10mA`, `current<5uA`, `digital D3 rising`,
`digital mask=0x0f value=0x05`, `uart D0 9600 "BOOT"`, `spi 0x9f,0x00`
(SPI channel config via `--spi-sclk` etc.). Incomplete captures (gaps,
interruption, trigger timeout) preserve partial data and exit 6.

## decode — offline

```bash
ppk2lab decode run.ppk2a --uart D0 --baud 9600
                [--data-bits N] [--parity none|even|odd] [--stop-bits 1|2]
                [--invert] [--msb-first]
ppk2lab decode run.ppk2a --spi-sclk D1 --spi-mosi D2 [--spi-miso D3]
                [--spi-cs D4] [--spi-mode 0-3] [--spi-word-bits N]
                [--spi-lsb-first] [--spi-cs-active-high] [--spi-clock-hz HZ]
# common: [--output ann.jsonl] [--overwrite] [--allow-experimental]
```

Emits annotations (JSONL with `--output`, inline otherwise) plus a kind/error
summary. Rates outside the validated tier warn (conditional), require
`--allow-experimental` (experimental), or are refused (unsupported).

## measure — offline

```bash
ppk2lab measure run.ppk2a                          # whole capture
ppk2lab measure run.ppk2a --window 0.1:0.25       # seconds
ppk2lab measure run.ppk2a --annotations ann.jsonl \
        --group-by annotation|kind|frame|transaction
# optional: --filtered            (adds spike-filtered statistics; raw is default)
#           --assume-voltage-mv MV (DUT supply voltage for energy)
```

Reports stored/missing/implausible samples with `covered_fraction`,
mean/min/max current, charge (uC) with `charge_is_lower_bound`, energy (uJ)
with its `voltage_basis` and `voltage_measured: false`, and the gaps inside
the window. Energy is `null` for an Ampere-mode capture unless
`--assume-voltage-mv` supplies the DUT's real supply voltage
(docs/energy-analysis.md).

## assert — offline

```bash
ppk2lab assert run.ppk2a \
  --rule 'after uart("TX_DONE"), within 20ms, avg_current < 10uA' \
  [--rule ...] [--rules-file rules.json] \
  [--uart-rx D0] [--baud 9600] [--spi-* ...] [--assume-voltage-mv MV] \
  [--format json|junit] [--output report] [--allow-experimental]
```

Rule DSL: `[after <event>,] [within <duration>,] <metric> <op> <value>`.
Events: `uart("TEXT"[, D2])`, `spi(0x9f, 0x00)`, `digital(D3 rising)`.
Metrics: `avg_current`/`mean_current`, `max_current`/`peak_current`,
`min_current`, `charge`, `energy`. Values need explicit units (`10uA`,
`5uC`, `1mJ`). Exit codes: 0 all passed; 1 failed or event not found; 6 an
evaluation window overlaps missing data.

## export — offline

```bash
ppk2lab export run.ppk2a --format csv   --output run.csv  [--filtered]
ppk2lab export run.ppk2a --format vcd   --output run.vcd  [--channels D0-D3]
ppk2lab export run.ppk2a --format jsonl --output run.jsonl
# common: [--overwrite]
```

CSV: one row per stored sample with raw current, range, counter, D0-D7,
validity, and a `gap_before_missing` marker. VCD: 10 us timescale with `x`
during gaps. JSONL: per-sample records with inline gap records. Exports are
derived views; the artifact remains the evidence.
