---
name: ppk2lab-maintain
description: Maintainer workflows for the ppk2lab repository itself — observation-only protocol research on PPK2 hardware behavior, and release-gate auditing before tagging or publishing. Not for using the CLI to take measurements.
---

# Maintaining the ppk2lab repository

Repo working rules live in `AGENTS.md`/`CLAUDE.md` and always apply: no
copied third-party source, no firmware binaries, no implicit hardware state
changes, no external publishing without explicit user authorization.

## Task → reference map

| The task involves | Read |
|---|---|
| probing firmware behavior, filling "Unverified items", producing hardware test vectors | [references/protocol-research.md](references/protocol-research.md) |
| release readiness review, pre-tag audit, version/schema/license gates | [references/release-gating.md](references/release-gating.md) |

For anything else (features, fixes, docs), follow the normal development
flow in `CONTRIBUTING.md` — no skill needed.
