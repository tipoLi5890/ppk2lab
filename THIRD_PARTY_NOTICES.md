# Third-Party Notices

This document is the dependency license audit for the `ppk2lab` `0.1.0`
release gate.

## Scope and policy

- **Scope**: the declared dependencies of the `ppk2lab` package, as recorded
  in `pyproject.toml` (`[project].dependencies`, `[project.optional-dependencies]`).
  It does not cover transitive dependencies pulled in only by dev tools
  (for example `attrs`, `jsonschema-specifications`, `referencing`, `rpds-py`
  behind `jsonschema`; `iniconfig`, `packaging`, `pluggy`, `pygments` behind
  `pytest`; `pathspec`, `mypy_extensions`, `typing_extensions`, `ast-serialize`,
  `librt` behind `mypy`), since none of those are shipped with `ppk2lab` and
  none are runtime dependencies of the published package.
- **Allowlist policy**: MIT, BSD (2-Clause or 3-Clause), Apache-2.0, and
  PSF-compatible licenses are pre-approved for both runtime and development
  use. Any dependency outside this allowlist requires explicit review before
  it can be added or upgraded.
- **Audit date**: 2026-08-19.
- **Re-verification**: this scan reflects the environment at audit time. It
  is re-run against the exact locked/installed versions at release-tag time;
  see `docs/releasing.md` for the release checklist. Any "to re-verify at tag
  time" entry below must be resolved before tagging `0.1.0`.

## Runtime dependencies

These are installed into every `ppk2lab` environment and are distributed
with the package.

| Package | Declared constraint | License (as verified) | Project URL | Allowlist verdict |
|---|---|---|---|---|
| pyserial | `>=3.5` | BSD (metadata `License: BSD`; classifier `License :: OSI Approved :: BSD License`; no specific clause count stated in metadata) — verified from `pyserial-3.5.dist-info/METADATA` | https://github.com/pyserial/pyserial | Allowed (BSD) |

## Optional dependencies

Installed only when the corresponding extra is requested (`pip install
"ppk2lab[numpy]"`).

| Package | Declared constraint | License | Project URL | Allowlist verdict |
|---|---|---|---|---|
| numpy | `>=1.26` | Not installed in the audited environment (no `numpy*.dist-info` present under `.venv/lib/python3.14/site-packages`); metadata could not be read. Well-known upstream license is BSD-3-Clause — **to re-verify at tag time** | https://github.com/numpy/numpy | Provisionally allowed (BSD, pending metadata verification) |

## Development dependencies (not shipped)

Installed only via the `dev` extra (`pip install "ppk2lab[dev]"`) for
contributors and CI. These are build/test/lint tooling and are **not
distributed with the `ppk2lab` package** on PyPI.

| Package | Declared constraint | License (as verified) | Project URL | Allowlist verdict |
|---|---|---|---|---|
| pytest | `>=8` | MIT (metadata `License-Expression: MIT`) — verified from `pytest-9.1.1.dist-info/METADATA` (installed: 9.1.1) | https://docs.pytest.org/en/latest/ | Allowed (MIT) |
| ruff | `>=0.6` | MIT (metadata `License-Expression: MIT`) — verified from `ruff-0.16.3.dist-info/METADATA` (installed: 0.16.3) | https://docs.astral.sh/ruff | Allowed (MIT) |
| mypy | `>=1.10` | MIT (metadata `License-Expression: MIT`) — verified from `mypy-2.3.1.dist-info/METADATA` (installed: 2.3.1) | https://www.mypy-lang.org/ | Allowed (MIT) |
| jsonschema | `>=4.21` | MIT (metadata `License-Expression: MIT`) — verified from `jsonschema-4.26.0.dist-info/METADATA` (installed: 4.26.0) | https://github.com/python-jsonschema/jsonschema | Allowed (MIT) |

## Python standard library

Python standard library usage throughout `ppk2lab` is covered by the PSF
license (Python Software Foundation License). No other third-party source
code is vendored into this repository; all third-party functionality is
consumed exclusively as installed dependencies listed above.

---

## Verdict summary

| Package | License | Verdict |
|---|---|---|
| pyserial | BSD | Allowed |
| numpy (optional) | BSD-3-Clause (well-known; not installed — to re-verify at tag time) | Provisionally allowed |
| pytest (dev) | MIT | Allowed |
| ruff (dev) | MIT | Allowed |
| mypy (dev) | MIT | Allowed |
| jsonschema (dev) | MIT | Allowed |

No blocked or unreviewed licenses were found among the declared
dependencies. The only open item for the `0.1.0` release gate is
re-verifying `numpy`'s license metadata directly once it is installed,
which should happen automatically as part of the re-scan at release-tag
time (`docs/releasing.md`).
