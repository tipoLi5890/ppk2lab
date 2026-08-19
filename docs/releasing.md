# Release procedure

Maintainer-only. This is a procedure, not a policy source: gates live in
`ROADMAP.md`, the read-only audit lives in
`skills/ppk2lab-maintain/references/release-gating.md`, and ground rules
live in `CONTRIBUTING.md`. Read all three first; nothing here overrides them.

## 1. Preconditions

- Every `0.1.0` release gate in `ROADMAP.md` ("`0.1.0` release gates") is
  green, including the hardware compatibility matrix and the hardware
  gates in the gating audit (OS matrix, firmware matrix, UART/SPI
  error-rate thresholds, soak tests). These cannot be faked or waived.
- The `ppk2lab-maintain` release-gating audit
  (`skills/ppk2lab-maintain/references/release-gating.md`) ends with
  **"ready to tag: yes"**, no blockers. On "no", fix the blockers and
  re-run — never proceed on partial evidence.

## 2. Rehearsal (no external writes)

Local and reversible only: no remote, no tag, no package index.

```bash
git clean -xdn && python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

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
(create it on the first release, update it otherwise) matches the current
dependency set against the MIT/BSD/Apache-2.0 allowlist — `ROADMAP.md` gate
6 and gating-audit gate 7.

If anything here fails, stop, fix it, and re-run the gating audit before
continuing.

## 3. Version and changelog

1. Bump `__version__` in `src/ppk2lab/_version.py`. Stays `0.1.0.devN`
   until every hardware gate passes; the first release clearing all gates
   becomes `0.1.0` — never `1.0.0`.
2. In `CHANGELOG.md`, rename `## [Unreleased]` to the release heading with
   today's date (e.g. `## [0.1.0] - YYYY-MM-DD`), keeping its entries.
3. If this release changes `SCHEMA_VERSION` or capture `format_version`,
   add a CHANGELOG migration note per the compatibility policy in
   `ROADMAP.md` — do not bump either silently.
4. Confirm version sync: `ppk2lab --version` (rehearsal venv) ==
   `_version.py` == the new CHANGELOG heading (gating-audit gate 4).
5. Commit locally. Still not a release action.

## 4. Tag and publish — requires explicit maintainer authorization

**Everything below is an external write** (remote, tag, GitHub release,
PyPI). Per `CLAUDE.md`, `AGENTS.md`, and `CONTRIBUTING.md`, none of it runs
without the maintainer's explicit, in-the-moment authorization, and only
after CI **and** the hardware release workflow are green on the exact tag
commit (`ROADMAP.md` gate 8). Not executed here — listed for the maintainer:

Publishing runs through PyPI **Trusted Publishing** (OIDC) via
`.github/workflows/release.yml` — no API tokens are stored anywhere.
One-time setup: register the trusted publisher on PyPI (project `ppk2lab`,
owner `tipoLi5890`, repository `ppk2lab`, workflow `release.yml`,
environment `pypi`; likewise on TestPyPI with environment `testpypi`) and
create the matching GitHub environments under Settings → Environments,
ideally with a required reviewer on `pypi`.

1. Rehearse the upload: run the Release workflow manually
   (`gh workflow run release.yml`) — it builds and publishes to **TestPyPI**
   only; verify with
   `pip install -i https://test.pypi.org/simple/ ppk2lab`.
2. `git tag -a v0.1.0 -m "ppk2lab 0.1.0"` on the Section 3 commit, then
   `git push origin main && git push origin v0.1.0`.
3. Wait for CI and the hardware workflow to both go green on the tag
   commit; never release on red or pending.
4. Create the GitHub release from the tag
   (`gh release create v0.1.0 --notes-from-tag`). Publishing the release
   triggers `release.yml`, which verifies the tag matches
   `src/ppk2lab/_version.py`, builds, `twine check`s, and uploads to PyPI
   through the `pypi` environment.
5. Verify: `pipx install ppk2lab` on a machine with no prior `-e`
   checkout, then `ppk2lab --version`.

## 5. Post-release

1. In a fresh venv, `pip install ppk2lab==0.1.0` from PyPI (not the local
   checkout) and re-run the Section 2 doctor/discover smoke test with
   `--simulate`, confirming it matches the rehearsal build.
2. Open a new `## [Unreleased]` section at the top of `CHANGELOG.md` for
   the next `0.1.0.devN` cycle.
3. Update `ROADMAP.md`: remove the work items this release closed, and
   fill in the hardware compatibility matrix rows it validated.
