"""docs/api-baseline.md is the frozen public surface; these tests pin it to the code.

The release gate diffs against that document, so a name present in one side and
absent from the other is a defect in whichever side is wrong -- and until now
nothing checked. Every assertion here is an equality in both directions and
names the exact difference, because "the sets differ" does not tell a
maintainer which side to fix.

The document is deliberately not shipped in the sdist, so these tests skip when
it is absent rather than failing an install-tree test run.
"""

from __future__ import annotations

import importlib
import inspect
import json
import pathlib
import re

import pytest

import ppk2lab
from ppk2lab.cli.main import main
from ppk2lab.schemas import SCHEMAS

BASELINE = pathlib.Path(__file__).resolve().parents[1] / "docs" / "api-baseline.md"

pytestmark = pytest.mark.skipif(
    not BASELINE.is_file(),
    reason="docs/ does not travel in the sdist; the baseline can only be diffed from a checkout",
)

# Numerals the document spells out. Only the range a command list can plausibly
# reach is covered; an unmapped word fails loudly instead of passing silently.
_NUMERALS = {
    "Ten": 10,
    "Eleven": 11,
    "Twelve": 12,
    "Thirteen": 13,
    "Fourteen": 14,
    "Fifteen": 15,
    "Sixteen": 16,
}

_BACKTICKED = re.compile(r"`([^`]+)`")


def _doc() -> str:
    return BASELINE.read_text(encoding="utf-8")


def _section(number: str) -> str:
    """The text of one `## <number>.` section, up to the next `## ` heading."""
    text = _doc()
    start = text.index(f"\n## {number}.")
    rest = text.index("\n## ", start + 1)
    return text[start:rest]


def _frozen_marker(section: str) -> str:
    """The "frozen for `X.Y.Z`:" marker as the document actually spells it.

    Matching the version literally would make a version bump look like doc
    drift, which is the one thing this file must not do.
    """
    match = re.search(r"subcommands, frozen for `[^`]+`:", section)
    assert match is not None, "section 3 no longer states which version its list is frozen for"
    return match.group(0)


def _comma_list(text: str, marker: str) -> list[str]:
    """Read the comma-separated backticked list that follows ``marker``.

    Stops at the first backticked token that is not simply the next comma-
    separated item, so prose after the list is never swept in.
    """
    position = text.index(marker) + len(marker)
    items: list[str] = []
    while (match := _BACKTICKED.search(text, position)) is not None:
        between = text[position : match.start()].strip()
        if between != ("," if items else ""):
            break
        items.append(match.group(1))
        position = match.end()
    return items


def _table_names(section: str) -> list[str]:
    """First-column backticked names of a markdown table, header row excluded."""
    names = []
    for line in section.splitlines():
        if not line.startswith("|"):
            continue
        cell = line.split("|")[1].strip()
        match = _BACKTICKED.fullmatch(cell)
        if match is not None:
            names.append(match.group(1))
    return names


def _differences(label: str, documented: set[str], actual: set[str]) -> str:
    missing_from_doc = sorted(actual - documented)
    missing_from_code = sorted(documented - actual)
    return (
        f"{label} drifted from docs/api-baseline.md:\n"
        f"  in the code but not documented: {missing_from_doc or 'none'}\n"
        f"  documented but not in the code: {missing_from_code or 'none'}"
    )


def _capabilities(capsys) -> dict:
    assert main(["--json", "capabilities"]) == 0
    return json.loads(capsys.readouterr().out)["result"]


def test_documented_exports_match_dunder_all():
    """Section 2's table is exactly `ppk2lab.__all__`, in both directions."""
    documented = _table_names(_section("2"))
    actual = set(ppk2lab.__all__)
    assert set(documented) == actual, _differences("__all__", set(documented), actual)


def test_documented_export_count_matches_its_own_table():
    """The "(N exports)" claim in section 2 counts the rows it introduces."""
    section = _section("2")
    stated = int(re.search(r"\((\d+) exports\)", section).group(1))
    assert stated == len(_table_names(section)), (
        f"section 2 says {stated} exports but tabulates {len(_table_names(section))}"
    )


def test_documented_commands_match_capabilities(capsys):
    """Section 3's frozen subcommand list is exactly what `capabilities` publishes."""
    section = _section("3")
    documented = _comma_list(section, _frozen_marker(section))
    actual = {command["name"] for command in _capabilities(capsys)["commands"]}
    assert set(documented) == actual, _differences("CLI subcommands", set(documented), actual)


def test_documented_command_count_matches_its_own_list():
    """The spelled-out count in section 3 counts the commands it then names."""
    section = _section("3")
    word = re.search(r"(\w+) subcommands, frozen", section).group(1)
    assert word in _NUMERALS, f"unmapped numeral {word!r} in section 3; extend _NUMERALS"
    listed = _comma_list(section, _frozen_marker(section))
    assert _NUMERALS[word] == len(listed), (
        f"section 3 says {word} ({_NUMERALS[word]}) subcommands but names {len(listed)}"
    )


def test_documented_schemas_match_capabilities(capsys):
    """Section 4's schema list is exactly what `capabilities` publishes."""
    documented = _comma_list(_section("4"), "served by `ppk2lab schema`):")
    actual = set(_capabilities(capsys)["schemas"])
    assert set(documented) == actual, _differences("schemas", set(documented), actual)
    assert actual == set(SCHEMAS), "capabilities disagrees with ppk2lab.schemas.SCHEMAS"


def test_documented_schema_count_matches_its_own_list():
    """The "(N, from ...)" claim in section 4 counts the schemas it then names."""
    section = _section("4")
    stated = int(re.search(r"\*\*Schemas\*\* \((\d+),", section).group(1))
    listed = _comma_list(section, "served by `ppk2lab schema`):")
    assert stated == len(listed), f"section 4 says {stated} schemas but names {len(listed)}"


def test_documented_version_and_schema_version_are_current():
    """Line "Current: ..." pins both version strings; a bump must update it."""
    text = _doc()
    version = re.search(r'`ppk2lab\.__version__ = "([^"]+)"`', text).group(1)
    schema_version = re.search(r'`ppk2lab\.SCHEMA_VERSION = "([^"]+)"`', text).group(1)
    assert version == ppk2lab.__version__, (
        f"baseline says __version__ {version!r}, package is {ppk2lab.__version__!r}"
    )
    assert schema_version == ppk2lab.SCHEMA_VERSION, (
        f"baseline says SCHEMA_VERSION {schema_version!r}, package is {ppk2lab.SCHEMA_VERSION!r}"
    )


def test_subpackage_level_names_resolve():
    """Section 4b names stable symbols that are not re-exported; each must import.

    Only one direction is checkable here: nothing in the code enumerates
    "stable but not in __all__", so this catches a rename or removal, not an
    undocumented addition.
    """
    # Both tables, because the second package's names live in 4a and only
    # there. Iterating 4b alone made the `ppk2lab_web.` arm below unreachable:
    # the prefix filter was fixed in 0.4.0, but the section it filters was
    # never one that contains a `ppk2lab_web.` row, so `static_dir` went
    # unchecked while the test reported green.
    for dotted in _table_names(_section("4a")) + _table_names(_section("4b")):
        # `ppk2lab_web.` has to be listed explicitly: it does not start with
        # `ppk2lab.`, so the obvious prefix test would skip every entry for the
        # second package while still reporting green.
        if not dotted.startswith(("ppk2lab.", "ppk2lab_web.")):
            continue  # e.g. `PPK2.firmware_fingerprint()`, covered by section 2
        module, _, attribute = dotted.rpartition(".")
        try:
            importlib.import_module(dotted)
            continue  # the name is itself a module
        except ImportError:
            pass
        obj = importlib.import_module(module)
        assert hasattr(obj, attribute), f"docs/api-baseline.md section 4b names missing {dotted}"


#: The module-level functions the baseline spells out as a full call form. An
#: explicit list beats a general parser: prose around these rows changes often,
#: and a parser that swallows a sentence would fail for a reason nobody can act
#: on. `PPK2.open` is here because every other constructor documented in the
#: file defers to its keywords.
_DOCUMENTED_CALLS = (
    "discover",
    "read_capture",
    "write_capture",
    "decode_capture",
    "read_window",
    "compute_stats",
    "PPK2.open",
)


def _resolve(name: str):
    from ppk2lab.capture import read_window
    from ppk2lab.capture.stats import compute_stats
    from ppk2lab.device import PPK2

    return {
        "discover": ppk2lab.discover,
        "read_capture": ppk2lab.read_capture,
        "write_capture": ppk2lab.write_capture,
        "decode_capture": ppk2lab.decode_capture,
        "read_window": read_window,
        "compute_stats": compute_stats,
        "PPK2.open": PPK2.open,
    }[name]


def _backticked_spans(text: str) -> list[str]:
    """Every inline code span, fence lines excluded."""
    return [
        match.group(1)
        for line in text.splitlines()
        if not line.startswith("```")
        for match in _BACKTICKED.finditer(line)
    ]


def _documented_parameters(name: str) -> list[str]:
    """The parameter tokens of the backticked `name(...)` form in the baseline.

    Scanning line by line, because a fenced block leaves an odd number of
    backticks behind and pairing them across the whole document then reads
    prose as code for everything after it.
    """
    for span in _backticked_spans(_doc()):
        if not span.startswith(f"{name}("):
            continue
        inside = span[len(name) + 1 : span.index(")")]
        assert "(" not in inside, f"nested call in the documented form of {name}: {span!r}"
        return [token.strip() for token in inside.split(",") if token.strip()]
    raise AssertionError(f"docs/api-baseline.md no longer states a call form for `{name}(...)`")


@pytest.mark.parametrize("name", _DOCUMENTED_CALLS)
def test_documented_signature_matches_the_code(name):
    """A documented default is a promise about behaviour, not decoration.

    `discover(*, simulate=False)` outlived the default becoming `None`, and
    nothing noticed: the names and counts pinned elsewhere in this file are
    identical either way, while the documented promise ("does not simulate
    unless asked") had become the opposite of what the code does.
    """
    documented = _documented_parameters(name)
    actual = inspect.signature(_resolve(name)).parameters

    names = [token.split("=", 1)[0] for token in documented if token != "*"]
    assert names == list(actual), (
        f"`{name}(...)` in docs/api-baseline.md takes {names}, the code takes {list(actual)}"
    )

    if "*" in documented:
        after_star = documented[documented.index("*") + 1 :]
        assert {token.split("=", 1)[0] for token in after_star} == {
            parameter
            for parameter, spec in actual.items()
            if spec.kind is inspect.Parameter.KEYWORD_ONLY
        }, f"`{name}(...)` documents the keyword-only boundary in the wrong place"

    for token in documented:
        parameter, _, shown = token.partition("=")
        # `start_index=` documents *that* there is a default, not which -- only a
        # spelled-out value is a claim this test can check.
        if not shown:
            continue
        default = actual[parameter].default
        assert default is not inspect.Parameter.empty, (
            f"`{name}(...)` documents a default for {parameter}, which has none"
        )
        assert shown in {repr(default), str(default)}, (
            f"docs/api-baseline.md says `{name}(..., {token})`, the code defaults it to {default!r}"
        )
