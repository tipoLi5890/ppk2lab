# Release gating (read-only audit)

Never tag, push, or publish — report readiness; the maintainer acts. The
authoritative gate list is `ROADMAP.md` ("`0.2.0` release gates"); the
procedure the maintainer follows afterwards is `docs/releasing.md`.

Version policy: the first stable release is `0.2.0`, cut once every gate
below passes. Everything before it is a `0.2.0.devN` pre-release that plain
`pip install ppk2lab` does not resolve. `0.1.0.dev0` is on PyPI and is
still the only build there — a historical fact, not a gate. The project is
never labeled `1.0.0` under the current plan.

## Preconditions — cheap, and they gate everything below

```bash
pytest
ruff check src tests && ruff format --check src tests
mypy
ppk2lab --simulate doctor --json
ppk2lab capabilities --json
ppk2lab schema --list
```

`--simulate doctor` must still exit 0. `doctor` now exits nonzero on a
failing check, so a nonzero exit here is a real regression rather than an
absent device. Version sync: `ppk2lab --version` == the value in
`src/ppk2lab/_version.py` == the CHANGELOG heading.

## The eight gates, in `ROADMAP.md` order

1. **Stability labels.** API, schemas, and capture format carry their
   stability and version policy (`docs/api-baseline.md`, `docs/SPEC.md`).
   `tests/test_api_baseline.py` diffs the baseline against
   `capabilities --json` in both directions, so an undocumented command —
   or a documented one that no longer exists — fails the suite rather than
   waiting for this audit. Removing or renaming a field, error code, or
   exit code needs a `SCHEMA_VERSION` bump plus a CHANGELOG migration note.
2. **Docs reproducible from a clean environment.** README/INSTALL/CLI
   examples run in a fresh venv (`--simulate` where there is no hardware);
   no dangling internal links, including the translated READMEs under
   `.github/`. `tests/test_docs_cli_surface.py` runs every documented
   `ppk2lab` invocation in `docs/`, the READMEs, `examples/` and `skills/`
   against the real parser, so a flag that exists only in prose fails the
   suite.
3. **At least one physical test pass on Windows, macOS, and Linux.** macOS
   (Apple silicon) passed on 2026-08-20; Windows and Linux are open.
4. **UART 9600 and SPI 10 kHz error rates** meet the pre-defined thresholds
   on the hardware fixture. "Validated" in the decoder tier table names the
   tier's intent, not a measured rate — confirming the validated tier is
   part of this gate, not an input to it.
5. **Provenance.** No source copied from other projects, no unlicensed
   firmware binaries; every fixture self-generated with its method
   recorded.
6. **Licenses.** Dependencies against the MIT/BSD/Apache-2.0 allowlist
   (`THIRD_PARTY_NOTICES.md`), each row verified from installed metadata.
7. **Skills and plugins.** Each `skills/*/SKILL.md` has valid frontmatter
   and its references resolve; the `version` in `.claude-plugin/plugin.json`
   matches the package version; installation and representative prompts
   verified on Claude Code and Codex.
8. **CI green on the tag commit, and the hardware gates validated on a
   physical device.** They have no CI workflow, and are not meant to: they
   need a PPK2 and an MCU fixture attached. `ROADMAP.md`'s compatibility
   matrix is the in-repo record of which configurations were covered; the
   bench evidence itself lives with the maintainer.

Gates 3 and 4, and the hardware half of gate 8, cannot be faked and cannot
be closed from a simulated run: `--simulate` exercises the toolchain, not
the instrument. Report them PENDING, and say which matrix cells are empty.

## What the first hardware session did and did not close

One physical session exists — one PPK2, one firmware fingerprint
(`HW=49625 IA=59.0 keys=40 ports=2`), macOS on Apple silicon. It covered
discovery, metadata and calibration, capture and loss accounting,
interruption and recovery, the `0.2.0` artifact commands, and DUT-power
lifetime. Treat it as one cell of gate 3 and nothing more:

- **Gate 3 stays PENDING.** macOS (Apple silicon) is recorded as passed for
  that fingerprint; Windows, Linux and macOS (Intel) have no pass at all,
  so the gate as written is not met.
- **Gate 4 stays PENDING** and is not close: there is no MCU fixture, so no
  UART or SPI error rate has been measured on real signals at all.
- **The hardware half of gate 8 stays PENDING**, as do multi-device, hot
  unplug, and the 8-24 h soak — the longest capture in the session was
  60 s.
- **Absolute accuracy remains unverified.** The one known-load check used a
  ±5% resistor: it rules out a gross error and cannot resolve the
  instrument's gain error. Never let it be recorded as a calibration
  cross-check.

The compatibility matrix is keyed on the firmware fingerprint, because the
measurement port reports no version string — one row per fingerprint
actually attached, and no row for a configuration nobody ran. Audit it that
way: an empty cell is the correct record of an unrun configuration, not a
hole to fill, and an observation that cannot be tied to a fingerprint is
unattributed rather than assignable to a row.

Output: a gate-by-gate table (gate, evidence, PASS/FAIL/PENDING) ending
with "ready to tag: yes/no (blockers: …)". Never mark a gate passed on
partial evidence.
