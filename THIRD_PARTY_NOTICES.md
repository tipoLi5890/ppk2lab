# Third-Party Notices

This document is the dependency license audit for the `ppk2lab` `0.5.0`
release gate.

## Scope and policy

- **Scope**: two things, because the distribution contains two kinds of
  third-party code. First, the declared dependencies of the `ppk2lab` package,
  as recorded in `pyproject.toml` (`[project].dependencies`,
  `[project.optional-dependencies]` — which since `0.5.0` includes the `web`
  extra). Second — since `0.4.0` — the npm
  packages **bundled into the shipped bits** of `ppk2lab_web/static/app.js`,
  which are not declared anywhere in `pyproject.toml` and would be missed by a
  scan that only reads it.
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
- **Audit date**: 2026-08-21.
- **Re-verification**: this scan reflects the environment at audit time. It
  is re-run against the exact locked/installed versions at release-tag time;
  see `docs/releasing.md` for the release checklist. Any "to re-verify at tag
  time" entry below must be resolved before tagging the release.

## Runtime dependencies

These are installed into every `ppk2lab` environment and are distributed
with the package.

| Package | Declared constraint | License (as verified) | Project URL | Allowlist verdict |
|---|---|---|---|---|
| pyserial | `>=3.5` | BSD (metadata `License: BSD`; classifier `License :: OSI Approved :: BSD License`; no specific clause count stated in metadata) — verified from `pyserial-3.5.dist-info/METADATA` | https://github.com/pyserial/pyserial | Allowed (BSD) |

## Optional runtime dependencies: the `web` extra

Installed only by `pip install "ppk2lab[web]"`, which is what `ppk2lab web`
needs. A plain `pip install ppk2lab` resolves to `pyserial` and nothing else,
and nothing in the `ppk2lab` package imports any of these. They are **not**
compiled into anything the wheel ships; they are ordinary dependencies resolved
at install time.

Deliberately *not* `uvicorn[standard]`: that adds `httptools`, `uvloop` and
`watchfiles`, none of which publishes a pure-Python or abi3 wheel, and `uvloop`
has no Windows wheel at all.

| Package | Declared constraint | License (as verified) | Project URL | Allowlist verdict |
|---|---|---|---|---|
| starlette | `>=1.0,<2` | BSD-3-Clause (metadata `License-Expression: BSD-3-Clause`, verified from `starlette-1.6.0.dist-info/METADATA`) | https://github.com/encode/starlette | Allowed (BSD) |
| uvicorn | `>=0.50` | BSD-3-Clause (metadata `License-Expression: BSD-3-Clause`, verified from `uvicorn-0.52.4.dist-info/METADATA`) | https://github.com/encode/uvicorn | Allowed (BSD) |
| websockets | `>=16` | BSD-3-Clause (metadata `License-Expression: BSD-3-Clause`, verified from `websockets-17.0.1.dist-info/METADATA`) | https://github.com/python-websockets/websockets | Allowed (BSD) |

Their transitive set, resolved and verified the same way:

| Package | Pulled in by | License (as verified) | Allowlist verdict |
|---|---|---|---|
| anyio | starlette | MIT | Allowed (MIT) |
| idna | anyio | BSD-3-Clause | Allowed (BSD) |
| sniffio | anyio | MIT / Apache-2.0 (dual) | Allowed |
| click | uvicorn | BSD-3-Clause | Allowed (BSD) |
| h11 | uvicorn | MIT | Allowed (MIT) |
| typing-extensions | starlette, anyio | PSF-2.0 | Allowed (PSF, permissive) |

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

## Bundled frontend dependencies

`ppk2lab_web/static/app.js` is a build artifact, committed and shipped so that
installing from PyPI never requires a Node toolchain. It is a bundle: the
libraries below are **compiled into that file** and are distributed inside every
wheel and sdist. Nothing in `pyproject.toml` mentions them, so a license scan
that reads only the Python metadata will not see them.

Versions and licenses are read from `webui/node_modules/<pkg>/package.json` at
audit time, the same standard as the tables above.

| Package | Version | License | Upstream | Verdict |
|---|---|---|---|---|
| react | 19.2.8 | MIT (`package.json` `"license": "MIT"`) | https://react.dev | Allowed (MIT) |
| react-dom | 19.2.8 | MIT (`package.json` `"license": "MIT"`) | https://react.dev | Allowed (MIT) |
| scheduler | 0.27.0 | MIT (`package.json` `"license": "MIT"`) | https://react.dev | Allowed (MIT) |

MIT requires its copyright notice to travel with every copy, and a minifier
will strip that notice unless told not to. `webui/vite.config.ts` therefore
sets `esbuild.legalComments: "eof"`, which appends the upstream `@license`
banners to the end of the bundle that actually ships. Verifying that is one
grep, and it belongs in the release re-scan:

```bash
grep -c "@license" src/ppk2lab_web/static/app.js   # must be > 0
```

Everything else under `webui/` — Vite, TypeScript, vitest, the React type
definitions — is a build-time tool. None of it is compiled into `app.js` and
none of it is distributed, so it sits outside this audit for the same reason
the Python dev extra's transitive dependencies do.

## Python standard library

Python standard library usage throughout `ppk2lab` is covered by the PSF
license (Python Software Foundation License). Apart from the bundled frontend
libraries listed above, no third-party source code is vendored into this
repository; all other third-party functionality is consumed exclusively as
installed dependencies.

---

## Verdict

No blocked or unreviewed licenses were found, in either scope. Every license
above was read from installed metadata rather than assumed, and no open items
remain for the `0.5.0` license gate. The re-scan at release-tag time
(`docs/releasing.md`) confirms the exact installed versions and re-runs the
`@license` grep above.

There is deliberately no separate summary table here. One used to restate the
rows above verbatim, which meant adding a dependency required editing three
tables and forgetting one of them was silent.

### Change history

- 2026-08-20: the `numpy` extra was removed from `pyproject.toml` — no
  module in the package ever imported NumPy, so nothing was accelerated by
  requesting it. Its row was the only entry that had been allowlisted on a
  well-known license instead of on metadata read from an installed
  distribution, so removing the extra closed the audit's last open item.
