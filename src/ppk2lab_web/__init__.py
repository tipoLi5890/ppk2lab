"""Web console for a PPK2: the server half.

This package sits beside :mod:`ppk2lab` rather than inside it because the core
driver must stay importable with `pyserial` as its only dependency. The server
arrives through the `web` extra -- ``pip install 'ppk2lab[web]'`` -- and the
core never imports anything from here.

What ships in the wheel unconditionally is :data:`STATIC_DIR`: the built
console, produced from the frontend sources in ``webui/`` by ``npm run build``
and committed alongside this package. Committing the build is deliberate:
someone installing from PyPI must not need a Node toolchain to serve the
console.

:func:`serve` and :func:`create_app` need the extra, and are imported lazily so
that this module -- and therefore :func:`static_dir` -- keeps working in a base
install. A missing optional dependency should be a clear error from
``ppk2lab web``, not a failure to import the package that holds the assets.

The process that :func:`serve` starts owns exactly one open
:class:`ppk2lab.PPK2` for its whole lifetime. It has to: a serial port is
exclusive, and DUT power does not survive the port closing, so a powered
measurement can only happen inside one session. That also means **closing the
browser tab does not de-energise VOUT** -- only stopping the server does.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .app import create_app as create_app
    from .server import serve as serve

#: :data:`STATIC_DIR` is the unchecked path and exists for tests and for the
#: server to build on; callers who want a directory they can serve should ask
#: for one that has been verified.
__all__ = ["create_app", "serve", "static_dir"]

#: Directory holding the built console (``index.html``, ``app.js``, ``app.css``).
#: Not validated -- prefer :func:`static_dir`.
STATIC_DIR: Path = Path(__file__).resolve().parent / "static"


def static_dir() -> Path:
    """Return the directory holding the built console.

    Raises :class:`FileNotFoundError` with the command that produces it rather
    than serving an empty directory: a console that answers 404 for its own
    JavaScript is harder to diagnose than one that refuses to start.
    """
    index = STATIC_DIR / "index.html"
    if not index.is_file():
        raise FileNotFoundError(
            f"the built console is missing from {STATIC_DIR}. A release wheel "
            "always contains it; a source distribution does not carry the "
            "frontend sources, so rebuilding needs a git checkout "
            "(`cd webui && npm install && npm run build`)."
        )
    return STATIC_DIR


def __getattr__(name: str) -> Any:
    """Import the server only when it is asked for.

    `tests/test_web_assets.py` imports this package unconditionally to check
    what the wheel carries, and CI installs without the extra in at least one
    configuration. An eager import would turn "the optional dependency is not
    installed" into a collection error in a suite that is not about the server
    at all.
    """
    if name == "serve":
        from .server import serve

        return serve
    if name == "create_app":
        from .app import create_app

        return create_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
