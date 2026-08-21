# Agent working rules for this repository

These rules apply to Codex, Claude Code, and any other tool-using agent
working *on* this repository. (For using the `ppk2lab` CLI as an agent, see
`docs/agent-interface.md` and `examples/agent_workflow.md`.)

## Before changing anything

1. Read `README.md`, `CLAUDE.md`, and the relevant sections of
   `docs/SPEC.md` and `docs/api-baseline.md`.
2. Inspect the working tree and preserve existing user changes.
3. State the narrow task being implemented and how it will be verified.
4. Do not expand scope because a related roadmap item exists.

## Hard constraints

- Publish only through `.github/workflows/release.yml` (GitHub Release →
  `pypi` environment → trusted publishing). Never upload by any other path,
  and never create a release or a tag without an explicit user request.
- Never copy, translate, or incorporate source code from other projects.
  PPK2 behavior comes from Nordic official materials (`docs/sources.md`).
- Never add code that enables DUT power, changes voltage, or resets
  hardware implicitly — including in tests, hooks, or skills.
- Do not describe the project as a port, fork, translation, or rewrite of
  another application.
- Do not add `provenance.md` or `clean-room.md`.

## Verification loop

```bash
pytest                       # 100% pass, no hardware required
ruff check src tests && ruff format --check src tests
mypy
ppk2lab --simulate doctor    # CLI smoke test
ppk2lab --simulate web --http-port 0   # the console; Ctrl-C exits 0
```

Touching `webui/` adds a second loop, and the rebuilt bundle has to be
committed with the change. Nothing rebuilds it at install time, so CI compares
the shipped bundle against its sources and fails when they disagree — a stale
console would otherwise ship with every other check green:

```bash
cd webui && npm run typecheck && npm test && npm run build
git status --porcelain -- ../src/ppk2lab_web/static   # commit what changed
```

A change is complete only when code, tests, schemas, docs, error behavior,
and hardware-safety implications agree (see "Definition of done" in
`CLAUDE.md`).
