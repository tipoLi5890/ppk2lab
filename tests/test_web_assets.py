"""The web console's shipped assets, and the boundary around them.

`ppk2lab_web` exists to hold a build artifact, which makes it exactly the kind
of thing that breaks quietly: a packaging change drops a file, every other test
stays green, and the failure only appears in a wheel someone already installed.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import ppk2lab_web

REQUIRED = ("index.html", "app.js", "app.css")


def test_static_dir_contains_the_built_console() -> None:
    directory = ppk2lab_web.static_dir()
    assert directory == ppk2lab_web.STATIC_DIR
    for name in REQUIRED:
        asset = directory / name
        assert asset.is_file(), f"{name} is missing from {directory}"
        assert asset.stat().st_size > 0, f"{name} is empty"


def test_index_loads_the_bundle_it_ships_with() -> None:
    """A console whose HTML points at a file that is not there is worse than none."""
    index = (ppk2lab_web.static_dir() / "index.html").read_text(encoding="utf-8")
    assert "app.js" in index
    assert "app.css" in index


def test_bundle_keeps_the_license_notices_it_is_required_to_carry() -> None:
    """MIT requires its copyright notice to travel with every copy.

    Minifiers strip license banners by default; `webui/vite.config.ts` turns
    that off. If someone turns it back on, the wheel starts shipping an
    unlicensed copy of React and nothing else would notice.
    """
    bundle = (ppk2lab_web.static_dir() / "app.js").read_text(encoding="utf-8")
    assert "@license" in bundle, "license banners were stripped from the shipped bundle"


def test_missing_assets_raise_with_the_way_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ppk2lab_web, "STATIC_DIR", tmp_path)
    with pytest.raises(FileNotFoundError) as excinfo:
        ppk2lab_web.static_dir()
    message = str(excinfo.value)
    assert str(tmp_path) in message
    assert "npm run build" in message


def test_only_the_validated_accessor_is_exported() -> None:
    """`STATIC_DIR` is the unchecked path; the export list must not advertise it."""
    assert ppk2lab_web.__all__ == ["static_dir"]


def test_importing_the_core_does_not_pull_in_the_console() -> None:
    """The core keeps `pyserial` as its only dependency, and this is what holds it.

    Run in a subprocess: by the time this test file is collected, `ppk2lab_web`
    is already imported, so an in-process check would prove nothing.
    """
    result = subprocess.run(
        [sys.executable, "-c", "import ppk2lab, sys; print('ppk2lab_web' in sys.modules)"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "False", "importing ppk2lab dragged in ppk2lab_web"
