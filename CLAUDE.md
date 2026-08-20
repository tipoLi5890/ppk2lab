# CLAUDE.md

Working agreement for Claude Code in this repository. Repo-wide agent rules
live in [AGENTS.md](AGENTS.md); this file adds project context and the
contracts that must not drift.

## Project identity

- One name everywhere: `ppk2lab` — GitHub repo (`tipoLi5890/ppk2lab`), PyPI
  package, Python import, and CLI executable.
- License: MIT (`LICENSE`), covering this repository's original material
  only; trademark and firmware boundaries are in `NOTICE.md`.
- Versioning: `0.2.0` is released and is the project's first stable
  version. Stable numbering covers the machine-readable contracts, not the
  hardware validation, which is partial and tracked in `ROADMAP.md`. Never
  label the project `1.0.0` under the current plan.
- Unofficial project, not affiliated with Nordic Semiconductor ASA. PPK2
  behavior is referenced from Nordic official documentation and the official
  Power Profiler app repository only (`docs/sources.md`). Never copy,
  translate, or incorporate source code from other projects, and never
  describe this project as a port, fork, translation, or rewrite.

## Current state

- The full planned feature set is implemented: device driver (discovery
  with measurement-port probing, interrupted-session recovery), loss-aware
  100 kS/s stream parser, calibration, canonical `.ppk2a` capture artifact,
  D0-D7 logic analysis + VCD, streaming UART/SPI decoders with enforced
  rate tiers, software triggers, per-event energy analysis, assertion DSL
  with JSON/JUnit reports, a 12-command CLI with `--simulate`, sync/async
  Python APIs, and versioned JSON schemas.
- Published: public GitHub repository with CI (Linux/macOS/Windows ×
  Python 3.11-3.14) and a PyPI development preview released through the
  trusted-publishing workflow.
- Remaining before `0.2.0`: hardware validation gates — see `ROADMAP.md`.
- The frozen public surface is `docs/api-baseline.md`; data model and
  stability policy are `docs/SPEC.md`.

## External writes

- Commit and push only for work the user asked for in this session; branch
  first if a change is exploratory.
- PyPI publishing happens only through `.github/workflows/release.yml`
  (GitHub Release → `pypi` environment approval by the maintainer). Never
  upload by any other path, and never create releases or tags without an
  explicit user request.

## Hardware safety contract

- Never enable DUT power, change source voltage or mode, or reset a device
  implicitly — not in code paths, tests, hooks, or skills. `configure` is a
  dry run without `--apply`; `capture` never touches DUT power.
- Voltage is explicit millivolts (`voltage_mv`), validated against device
  limits before any byte reaches the wire; never inferred from a DUT name.
- Every state change reports requested/before/after state and whether the
  after state was read back. Sessions restore the starting power state on
  close (fail-safe OFF with a recorded warning when unknown).

## Engineering expectations

- Python ≥ 3.11; the only runtime dependency is `pyserial`.
- Keep transport, protocol, calibration, capture, decoders, analysis, CLI,
  and agent adapters separated; unit tests use the mock transport and never
  require hardware. State-changing hardware tests stay separate.
- Raw capture data is the source of truth; derived exports never replace
  it. Preserve unknown values; never invent defaults; never hide data loss
  — gaps, warnings, or zero-confidence annotations must surface wherever
  data can drop.
- New or changed behavior needs tests covering framing, chunk boundaries,
  counter wrap, gaps, calibration ranges, and malformed input as relevant.
- Test vectors are self-generated (`ppk2lab.testing` or our own hardware
  fixtures) with the generation method recorded.

## Documentation rules

- `README.md` (English) is canonical and stays entry-level; details live
  under `docs/`. The translations `.github/README.zh-Hant.md`,
  `README.zh-Hans.md`, and `README.ja.md` must be updated in the same
  change whenever README content changes (section parity; absolute GitHub
  URLs, because PyPI renders `README.md` as the long description and
  relative links 404 there; localized code-block comments are the
  established style).
- Machine-readable contracts (CLI JSON, schemas, exit codes, capture
  format) are frozen by `docs/api-baseline.md`; changes follow the
  stability policy in `docs/SPEC.md` plus a CHANGELOG entry.
- Retain this acknowledgment near the end of the README:
  `This project is developed with assistance from Claude Code and OpenAI Codex.`

## Verification loop

```bash
pytest                                   # no hardware required
ruff check src tests && ruff format --check src tests
mypy
ppk2lab --simulate doctor --json         # CLI smoke test
```

## Before making changes

1. Read `README.md`, this file, and the relevant parts of `docs/SPEC.md`.
2. Inspect the working tree and preserve existing user changes.
3. State the narrow task and its verification method; do not expand scope
   because a related roadmap item exists.

## Definition of done

A change is complete only when its code, tests, schemas, CLI/API
documentation (including README translations), error behavior, and
hardware-safety implications agree — and the verification loop is green.
