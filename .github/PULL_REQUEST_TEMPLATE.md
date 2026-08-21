# Pull request

## Summary

<!-- What does this change do, and why? State the narrow task. -->

## Linked issue

Closes #

## Type of change

- [ ] fix (bug fix)
- [ ] feature (new capability)
- [ ] docs
- [ ] refactor
- [ ] test
- [ ] ci

## How tested

```bash
pytest
ruff check src tests && ruff format --check src tests
mypy
ppk2lab --simulate doctor --json
ppk2lab --simulate web --http-port 0

# only if this change touches webui/:
cd webui && npm run typecheck && npm test && npm run build
```

<!-- If the change affects hardware behavior: which PPK2 firmware, OS, and
     wiring did you test on? Attach the doctor output. -->

## Hardware-safety implications

<!-- Does this change touch DUT power, source voltage, mode, reset, or any
     state-changing path? If yes, explain how requested/before/after state
     and readback are preserved. Write "none" if purely offline. -->

## Checklist

- [ ] Tests pass locally (`pytest`), including new tests for new behavior
      (framing, chunk boundaries, gaps, and malformed input where relevant).
- [ ] No source code copied, translated, or incorporated from other
      projects; PPK2 behavior referenced from Nordic official materials
      only (`docs/sources.md`).
- [ ] Fixtures and captures are self-generated (`ppk2lab.testing` or our
      own hardware) with the generation method recorded.
- [ ] No code path enables DUT power, changes voltage, or resets hardware
      implicitly; data loss is never hidden (gaps, warnings, or error
      annotations are emitted wherever data can drop).
- [ ] JSON schemas, error codes, exit codes, and docs updated together with
      the change; CHANGELOG entry added for contract changes.
- [ ] If `webui/` changed, the rebuilt bundle under `src/ppk2lab_web/static/`
      is committed with it — CI fails when the shipped console and its
      sources disagree.
- [ ] New dependencies (if any) are MIT/BSD/Apache-2.0-compatible.
- [ ] Commit messages follow Conventional Commits (e.g. `fix:`, `feat:`,
      `docs:`).
