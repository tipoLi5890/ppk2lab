# Release procedure

Maintainer-only. This is a procedure, not a policy source: gates live in
`ROADMAP.md`, the read-only audit lives in
`skills/ppk2lab-maintain/references/release-gating.md`, and ground rules
live in `CONTRIBUTING.md`. Read all three first; nothing here overrides them.

## 1. Preconditions

- Every release gate in `ROADMAP.md` ("`0.2.0` release gates") is green,
  including the gates that need a bench: a physical pass on Windows, macOS,
  and Linux (gate 3 — macOS on Apple silicon passed 2026-08-20, the other two
  are open), the UART 9600 / SPI 10 kHz error-rate thresholds on the fixture
  (gate 4), and the multi-device, hot-unplug and soak columns of the hardware
  compatibility matrix. These cannot be faked or waived, and `--simulate`
  cannot close any of them.
- The `ppk2lab-maintain` release-gating audit
  (`skills/ppk2lab-maintain/references/release-gating.md`) ends with
  **"ready to tag: yes"**, no blockers. On "no", fix the blockers and
  re-run — never proceed on partial evidence.

## 2. Rehearsal (no external writes)

Local and reversible only: no remote, no tag, no package index.

```bash
git clean -xdn && python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]" build   # build is a maintainer tool, not an extra

pytest
ruff check src tests
ruff format --check src tests
mypy

python -m build

# fresh-venv wheel install, not -e
deactivate
python -m venv /tmp/ppk2lab-release-check
source /tmp/ppk2lab-release-check/bin/activate
pip install dist/ppk2lab-*.whl
ppk2lab --version
PPK2LAB_SIMULATE=1 ppk2lab doctor --json
```

Reproduce the README quickstart commands with `--simulate` /
`PPK2LAB_SIMULATE=1` against this wheel (`discover`, `info`, `capture`,
`decode`), per `ROADMAP.md` gate 2. Then deactivate and remove the temp venv.

Re-run the dependency license scan and confirm `THIRD_PARTY_NOTICES.md`
matches the current dependency set against the MIT/BSD/Apache-2.0 allowlist,
each row verified from installed metadata — `ROADMAP.md` gate 6 and
gating-audit gate 6. The only runtime dependency is `pyserial`; the `dev`
extra is the rest of the audited set.

If anything here fails, stop, fix it, and re-run the gating audit before
continuing.

## 3. Version and changelog

1. Bump `__version__` in `src/ppk2lab/_version.py`. `0.2.0` was the first
   stable release; subsequent versions follow the compatibility policy in
   `ROADMAP.md` — never `1.0.0` under the current plan. A release whose
   hardware validation is still partial must say so in the README, in
   `ROADMAP.md` and in the CHANGELOG entry, rather than letting a stable
   number imply gates that have not passed.
2. In `CHANGELOG.md`, rename `## [Unreleased]` to the release heading with
   today's date (e.g. `## [0.2.0] - YYYY-MM-DD`), keeping its entries.
3. If this release changes `SCHEMA_VERSION` or capture `format_version`,
   add a CHANGELOG migration note per the compatibility policy in
   `ROADMAP.md` — do not bump either silently.
4. Confirm version sync: `ppk2lab --version` (rehearsal venv) ==
   `_version.py` == the new CHANGELOG heading — the gating audit's
   preconditions check the same three.
5. Commit locally. Still not a release action.

## 4. Tag and publish — requires explicit maintainer authorization

**Everything below is an external write** (remote, tag, GitHub release,
PyPI). Per `CLAUDE.md`, `AGENTS.md`, and `CONTRIBUTING.md`, none of it runs
without the maintainer's explicit, in-the-moment authorization, and only
after CI is green on the exact tag commit and the hardware gates have been
validated on a physical device (`ROADMAP.md` gate 8). Not executed here
— listed for the maintainer:

Publishing runs through PyPI **Trusted Publishing** (OIDC) via
`.github/workflows/release.yml` — no API tokens are stored anywhere.
One-time setup (already done for this repository): the trusted publisher is
registered on PyPI (project `ppk2lab`, owner `tipoLi5890`, repository
`ppk2lab`, workflow `release.yml`, environment `pypi`), and the matching
GitHub environment `pypi` exists under Settings → Environments with a
required reviewer, so every upload needs a manual approval click.

1. `git tag -a v0.2.0 -m "ppk2lab 0.2.0"` on the Section 3 commit, then
   `git push origin main && git push origin v0.2.0`.
2. Wait for CI to go green on the tag commit; never release on red or
   pending. The hardware gates have no workflow — they need a physical PPK2
   and an MCU fixture attached — so confirm instead that they were validated
   against this commit and that `ROADMAP.md`'s compatibility matrix records
   the configurations covered.
3. Create the GitHub release from the tag
   (`gh release create v0.2.0 --notes-from-tag`). Publishing the release
   triggers `release.yml`, which verifies the tag matches
   `src/ppk2lab/_version.py`, builds, `twine check`s, asserts the wheel it is
   about to upload carries `py.typed` and `License-Expression: MIT`, and —
   after the `pypi` environment approval — uploads to PyPI.
4. Verify: `pipx install ppk2lab` on a machine with no prior `-e`
   checkout, then `ppk2lab --version`.

## 5. Post-release

1. In a fresh venv, `pip install ppk2lab==0.2.0` from PyPI (not the local
   checkout) and re-run the Section 2 doctor/discover smoke test with
   `--simulate`, confirming it matches the rehearsal build.
2. Open a new `## [Unreleased]` section at the top of `CHANGELOG.md` for
   the next cycle.
3. Update `ROADMAP.md`: remove the work items this release closed, and fill
   in the compatibility-matrix cells it validated — one row per firmware
   fingerprint actually attached, never a row for a configuration nobody ran.
