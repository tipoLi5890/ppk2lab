# Security model

This document describes the threat model and hardening rules for ppk2lab
across four surfaces: hardware control, USB/serial input, capture files, and
untrusted analysis input.

## Hardware safety (protecting the DUT and operator)

The most damaging failure this tool can cause is powering or over-volting a
device under test. Hard rules, enforced in code and tests:

- No code path enables DUT power, changes source voltage, changes mode, or
  resets the device implicitly. Installation, discovery, `doctor`, and
  `capture` never do; `configure` is a dry run without `--apply`.
- Voltage is always explicit millivolts (`voltage_mv`); bare or unit-less
  values are rejected, and the range (800-5000 mV) is validated before any
  byte reaches the wire.
- Every state change records requested/before/after state and whether the
  after state was read back from the device.
- Sessions restore the starting DUT power state on close. When the starting
  state was unknown, power is set OFF as a fail-safe and the decision is
  recorded, never silent.
- There is no `--yes-to-everything` option and no public API to bypass
  voltage limits.

## USB / serial input (untrusted device data)

Data arriving from the serial port is treated as untrusted:

- The sample parser is bounded-memory, tolerates arbitrary chunk sizes,
  and treats malformed values (invalid range fields) as flagged data, never
  as crashes.
- Metadata parsing never raises on malformed content; unknown keys, NaN, and
  truncation are preserved and reported as warnings.
- The simulator (used with `--simulate`) refuses unknown opcodes loudly so
  protocol drift is caught in tests.

## Capture files (untrusted file input)

`.ppk2a` files may come from other machines or users:

- Readers validate the container, format version, sample encoding, chunk
  sizes, CRC32 per chunk, and the SHA-256 of the sample data before use.
- A file with a newer `format_version` is refused explicitly rather than
  misread.
- Malformed annotation JSONL is rejected with the line number; entries are
  never partially applied.
- Writers are atomic (temp file + rename) and never overwrite an existing
  file without `--overwrite`.

## Analysis input (rules, specs, CLI arguments)

- Assertion rules and trigger specs are parsed with strict grammars; parse
  failures return `INVALID_ARGUMENT` with remediation, never arbitrary
  evaluation. There is no `eval` of user input anywhere.
- Data loss can never produce a false success: assertion windows that
  overlap gaps return status `incomplete` (exit 6), and decoders emit
  zero-confidence error annotations rather than fabricated data.

## Reporting a vulnerability

Report security issues through
[GitHub Issues](https://github.com/tipoLi5890/ppk2lab/issues) (or a private
security advisory on the repository once published). Include the ppk2lab
version, OS, and reproduction steps. Hardware-safety regressions (anything
that could power a DUT unexpectedly) are treated at the highest severity.
