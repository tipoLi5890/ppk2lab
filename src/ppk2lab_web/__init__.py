"""Web console for a PPK2: the server half.

This package sits beside :mod:`ppk2lab` rather than inside it because the core
driver must stay importable with `pyserial` as its only dependency. When the
server lands its dependencies will arrive through a `web` extra; **there is no
such extra yet**, and `pip install 'ppk2lab[web]'` would warn and quietly
install the base package. The core never imports anything from here.

What ships in the wheel is :data:`STATIC_DIR` — the built console, produced from
the frontend sources in ``webui/`` by ``npm run build`` and committed alongside
this package. Committing the build is deliberate: someone installing from PyPI
must not need a Node toolchain to serve the console.

The HTTP/WebSocket server that owns the one open :class:`ppk2lab.PPK2` session
is not implemented yet. Until it is, this package exists to hold the built
assets and to fix the import path they will be served from.
"""

from __future__ import annotations

from pathlib import Path

#: Only ``static_dir`` is exported. :data:`STATIC_DIR` is the unchecked path and
#: exists for tests and for the server to build on; callers who want a directory
#: they can serve should ask for one that has been verified.
__all__ = ["static_dir"]

#: Directory holding the built console (``index.html``, ``app.js``, ``app.css``).
#: Not validated — prefer :func:`static_dir`.
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
