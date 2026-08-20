"""Every `ppk2lab ...` line in the documentation runs against the real parser.

Documentation is the only interface some readers ever see, and one of them is
followed at a bench with hardware attached: a flag that silently stops existing
costs a measurement session, not a retry. Nothing else pins the examples in
`docs/`, the READMEs, or the agent skills to the live CLI, so a renamed flag can
survive every other check in the suite.

Only names are checked, never values -- a placeholder like `<serial>` is exactly
what a documented example should contain.
"""

from __future__ import annotations

import argparse
import pathlib
import re

import pytest

from ppk2lab.cli.main import build_parser

ROOT = pathlib.Path(__file__).resolve().parents[1]

#: Documentation trees whose command lines must be real. `docs/` is not shipped
#: in the sdist, so an install-tree run finds nothing and skips.
DOC_GLOBS = ("docs/**/*.md", "README.md", ".github/README*.md", "skills/**/*.md", "examples/*.md")

#: A command line ends where the shell takes over.
_TERMINATORS = re.compile(r"\s(?:\||>|>>|&&|;|#)")

#: Flags a documented example may name that the parser cannot know about:
#: environment-style placeholders and the argparse built-in.
_ALWAYS_VALID = frozenset({"--help"})


def _doc_files() -> list[pathlib.Path]:
    found: list[pathlib.Path] = []
    for pattern in DOC_GLOBS:
        found.extend(sorted(ROOT.glob(pattern)))
    return found


_FENCE = re.compile(r"^```", re.MULTILINE)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")


def _clean(candidate: str) -> str | None:
    """Normalize one candidate invocation, or None if it is not a CLI call."""
    line = candidate.strip()
    if line.startswith("$ "):
        line = line[2:].strip()
    # `ppk2lab.capture.stats.compute_stats(...)` is a Python path, not a command.
    if not re.match(r"^ppk2lab(\s|$)", line):
        return None
    cut = _TERMINATORS.search(line)
    if cut is not None:
        line = line[: cut.start()]
    return line.strip()


def _command_lines(text: str) -> list[str]:
    """Every `ppk2lab ...` invocation in a document.

    Only fenced code blocks and whole inline code spans count. Prose that
    happens to begin with the word "ppk2lab" is not an invocation, and treating
    it as one would make this test noise rather than a contract.
    """
    joined = re.sub(r"\\\n\s*", " ", text)
    found: list[str] = []

    fences = [m.start() for m in _FENCE.finditer(joined)]
    for opening, closing in zip(fences[0::2], fences[1::2], strict=False):
        block = joined[joined.index("\n", opening) + 1 : closing]
        for raw in block.splitlines():
            cleaned = _clean(raw)
            if cleaned is not None:
                found.append(cleaned)

    # Inline spans, ignoring the ones inside the fenced blocks already scanned.
    outside = _FENCE.split(joined)[0::2] if fences else [joined]
    for chunk in outside:
        for match in _INLINE_CODE.finditer(chunk):
            cleaned = _clean(match.group(1))
            if cleaned is not None:
                found.append(cleaned)
    return found


def _parsed(line: str) -> tuple[str | None, list[str]]:
    """The subcommand a line invokes and every long flag it names."""
    tokens = line.split()[1:]  # drop "ppk2lab"
    command = None
    for token in tokens:
        if not token.startswith("-"):
            command = token
            break
    flags = [t.split("=", 1)[0] for t in tokens if t.startswith("--")]
    return command, flags


def _surface() -> tuple[dict[str, set[str]], set[str]]:
    """{command: its option strings} and the globally accepted option strings."""
    parser = build_parser()
    globals_: set[str] = {
        flag for action in parser._actions for flag in action.option_strings
    } | _ALWAYS_VALID
    per_command: dict[str, set[str]] = {}
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        for name, subparser in action.choices.items():
            per_command[name] = {
                flag for sub in subparser._actions for flag in sub.option_strings
            } | globals_
    return per_command, globals_


ALL_LINES = [
    (path.relative_to(ROOT).as_posix(), line)
    for path in _doc_files()
    for line in _command_lines(path.read_text(encoding="utf-8"))
]

pytestmark = pytest.mark.skipif(
    not ALL_LINES,
    reason="no documentation tree here (docs/ does not travel in the sdist)",
)


#: Built once: 248 documented lines would otherwise rebuild the parser 248 times.
SURFACE = _surface()


@pytest.mark.parametrize(("source", "line"), ALL_LINES)
def test_documented_command_line_is_real(source, line):
    per_command, globals_ = SURFACE
    command, flags = _parsed(line)
    if command is None:
        # `ppk2lab --version` and friends: no subcommand, only global flags.
        unknown = [f for f in flags if f not in globals_]
        assert not unknown, f"{source}: `{line}` names global flag(s) that do not exist: {unknown}"
        return
    assert command in per_command, (
        f"{source}: `{line}` invokes `ppk2lab {command}`, which is not a subcommand "
        f"(have: {', '.join(sorted(per_command))})"
    )
    unknown = [f for f in flags if f not in per_command[command]]
    assert not unknown, f"{source}: `{line}` names flag(s) `{command}` does not accept: {unknown}"


#: The documents that enumerate the assertion metric vocabulary. `--rule` takes
#: a metric name and nothing else publishes the list, so a metric named in
#: neither document is one a reader can only find by opening the evaluator.
METRIC_DOCS = ("docs/cli-reference.md", "skills/ppk2lab-operate/references/regression.md")


def _backticked_words(text: str) -> str:
    """Every inline code span, joined -- prose mentions do not count as documentation."""
    return " ".join(match.group(1) for match in _INLINE_CODE.finditer(text))


@pytest.mark.parametrize("source", METRIC_DOCS)
def test_documented_metrics_are_the_ones_the_evaluator_accepts(source):
    """The private table is the authority; the doc lists are hand-maintained.

    Adding a metric to the evaluator without adding it here leaves a rule that
    works but that nobody writes -- which is how `p5_current` and `p95_current`
    shipped invisible.
    """
    from ppk2lab.analysis.assertions import _METRICS

    path = ROOT / source
    if not path.is_file():
        pytest.skip(f"{source} is not present in this tree")
    spans = _backticked_words(path.read_text(encoding="utf-8"))
    missing = sorted(
        metric for metric in _METRICS if not re.search(rf"(?<!\w){metric}(?!\w)", spans)
    )
    assert not missing, f"{source} never names assertion metric(s): {missing}"
