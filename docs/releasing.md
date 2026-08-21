# Release procedure

Maintainer-only. This is a procedure, not a policy source: gates live in
`ROADMAP.md`, the read-only audit lives in
`skills/ppk2lab-maintain/references/release-gating.md`, and ground rules
live in `CONTRIBUTING.md`. Read all three first; nothing here overrides them.

## 1. Preconditions

- Every release gate in `ROADMAP.md` ("Release gates") that does not need a
  bench is green: the stability labelling, the reproducible examples, the
  licence allowlist scan across both dependency scopes, the skill and plugin
  check, and CI green on the tag commit.
- The gates that **do** need a bench — a physical pass on Windows, macOS and
  Linux (gate 3; macOS on Apple silicon passed 2026-08-20), the UART 9600 /
  SPI 10 kHz error-rate thresholds on the fixture (gate 4), and the
  multi-device, hot-unplug and soak columns of the compatibility matrix — are
  **recorded, not waived**. They have not blocked a release since `0.2.0` and
  do not block this one; what they require instead is that `ROADMAP.md`, the
  README and the CHANGELOG entry each state plainly what has and has not been
  validated, so a stable version number never implies a gate that has not
  passed. `--simulate` closes none of them and must never be recorded as
  though it had.

  This is the policy `CLAUDE.md` states and the one every release so far has
  followed. It used to be written here as "these cannot be faked or waived",
  which read as a hard block and was contradicted by every tag in the
  project's history — the rule against faking is real, the implied rule
  against shipping was not.
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

Confirm the committed frontend build matches its sources. Nothing at install
time rebuilds it, so a stale bundle would ship with every other check green:

```bash
cd webui && npm ci && npm run build && cd ..
git diff --exit-code -- src/ppk2lab_web/static
unzip -l dist/ppk2lab-*.whl | grep ppk2lab_web/static
```

Confirm the base install is still what the README promises, and that the web
command is honest without its extra — both from the wheel, because neither can
be fixed after an upload:

```bash
python -c "
from importlib.metadata import metadata
m = metadata('ppk2lab')
base = [r for r in (m.get_all('Requires-Dist') or []) if 'extra ==' not in r]
assert base == ['pyserial>=3.5'], base
"
# Expect exit 7 and WEB_EXTRA_MISSING, naming the packages that are missing:
ppk2lab --json web
echo $?

pip install 'ppk2lab[web]'
# Open it in a browser, then Ctrl-C: exits 0 with the session's audit record.
ppk2lab --simulate web --http-port 0
```

Reproduce the README quickstart commands with `--simulate` /
`PPK2LAB_SIMULATE=1` against this wheel (`discover`, `info`, `capture`,
`decode`), per `ROADMAP.md` gate 2. Then deactivate and remove the temp venv.

Re-run the dependency license scan and confirm `THIRD_PARTY_NOTICES.md`
matches the current dependency set against the MIT/BSD/Apache-2.0 allowlist,
each row verified from installed metadata — `ROADMAP.md` gate 6 and
gating-audit gate 6. Three scopes: the core's runtime dependency (`pyserial`), the `web` extra
(Starlette, uvicorn, websockets and their transitive set), **and** the npm
packages compiled into `ppk2lab_web/static/app.js`, which no Python metadata
mentions at all. Confirm their
notices survived minification:

```bash
grep -c "@license" src/ppk2lab_web/static/app.js   # must be > 0
```

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
4. Bump `"version"` in `.claude-plugin/plugin.json` to match. Gate 7 requires
   it and it was missed at both `0.2.0` and `0.3.0`, which is why the release
   workflow now fails on a mismatch rather than trusting this step.
5. Confirm version sync: `ppk2lab --version` (rehearsal venv) ==
   `_version.py` == `.claude-plugin/plugin.json` == the new CHANGELOG heading
   — the gating audit's preconditions check the same set.
6. Commit locally. Still not a release action.

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

1. `git tag -a vX.Y.Z -m "ppk2lab X.Y.Z"` on the Section 3 commit, then
   `git push origin main && git push origin vX.Y.Z`.
2. Wait for CI to go green on the tag commit; never release on red or
   pending. The hardware gates have no workflow — they need a physical PPK2
   and an MCU fixture attached — so confirm instead that they were validated
   against this commit and that `ROADMAP.md`'s compatibility matrix records
   the configurations covered.
3. Create the GitHub release from the tag
   (`gh release create vX.Y.Z --notes-from-tag`). Publishing the release
   triggers `release.yml`, which verifies the tag matches
   `src/ppk2lab/_version.py`, builds, `twine check`s, asserts the wheel it is
   about to upload carries `py.typed` and `License-Expression: MIT`, and —
   after the `pypi` environment approval — uploads to PyPI.
4. Verify: `pipx install ppk2lab` on a machine with no prior `-e`
   checkout, then `ppk2lab --version`.

## 5. Post-release

1. In a fresh venv, `pip install ppk2lab==X.Y.Z` from PyPI (not the local
   checkout) and re-run the Section 2 doctor/discover smoke test with
   `--simulate`, confirming it matches the rehearsal build.
2. Open a new `## [Unreleased]` section at the top of `CHANGELOG.md` for
   the next cycle.
3. Update `ROADMAP.md`: remove the work items this release closed, and fill
   in the compatibility-matrix cells it validated — one row per firmware
   fingerprint actually attached, never a row for a configuration nobody ran.
