# Release gating (read-only audit)

Never tag, push, or publish — report readiness; the maintainer acts.

Run and record each gate (full list: `ROADMAP.md`):

1. **Quality**: `pytest`, `ruff check src tests`,
   `ruff format --check src tests`, `mypy` — all green.
2. **CLI contract**: `ppk2lab --simulate doctor`,
   `ppk2lab capabilities --json`, `ppk2lab schema --list`; every command in
   `docs/cli-reference.md` exists in capabilities output and vice versa.
3. **Schema stability**: schemas validate (tests/test_schemas.py); removed
   or renamed fields/error codes/exit codes require a SCHEMA_VERSION bump
   plus a CHANGELOG migration note.
4. **Version sync**: `ppk2lab --version` == `src/ppk2lab/_version.py` ==
   CHANGELOG heading; stays `0.1.0.devN` until every hardware gate passes;
   never labeled 1.0.0.
5. **Docs**: README/INSTALL quickstart reproducible from a clean venv
   (`--simulate` where no hardware); no dangling internal links (including
   the translated READMEs under `.github/`).
6. **Provenance**: no copied third-party source, no firmware binaries; all
   fixtures self-generated with method notes.
7. **Licenses**: dependencies against the MIT/BSD/Apache-2.0 allowlist.
8. **Skills/plugins**: each `skills/*/SKILL.md` has valid frontmatter and
   its references resolve; plugin manifest version matches the package.
9. **Hardware gates** (cannot be faked): OS matrix, firmware matrix,
   UART 9600 / SPI 10 kHz error-rate thresholds, soak tests — PENDING
   blocks `0.1.0`.

Output: a gate-by-gate table (gate, evidence, PASS/FAIL/PENDING) ending
with "ready to tag: yes/no (blockers: …)". Never mark a gate passed on
partial evidence.
