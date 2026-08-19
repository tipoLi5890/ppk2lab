"""Atomic replacement for derived export files.

An export is proportional to the capture — a one-hour capture is tens of
gigabytes of CSV — so running out of space part-way through is the ordinary
failure here, not an exotic one. Writing straight to the destination destroys
whatever was there the instant the handle opens and leaves a truncated file
that reads exactly like a complete export. Every exporter therefore writes to
a temp file beside the destination and replaces the destination only once the
whole export is on disk; a failure leaves the previous file untouched.

This mirrors the discipline ``capture/artifact.py``'s ``ArtifactWriter`` uses
for the canonical ``.ppk2a`` artifact.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from io import TextIOWrapper
from pathlib import Path

from ..errors import OutputExistsError


@contextmanager
def atomic_write(
    path: str | os.PathLike[str],
    *,
    overwrite: bool = False,
    encoding: str = "utf-8",
    newline: str | None = None,
) -> Iterator[TextIOWrapper]:
    """Yield a text handle whose contents replace *path* on a clean exit.

    Anything raised inside the block — an I/O error, ``KeyboardInterrupt``, or
    a refusal the caller raises after writing rows — removes the temp file and
    leaves the destination exactly as it was.
    """
    target = Path(path)
    if target.exists() and not overwrite:
        raise OutputExistsError(f"output file exists: {target}")
    # Beside the destination, not in a temp dir: os.replace is only atomic
    # within one filesystem.
    tmp = target.parent / f".{target.name}.tmp{os.getpid()}"
    try:
        # The handle must be closed before the rename: a buffered tail still in
        # memory is exactly the data whose loss this guard exists to prevent.
        with open(tmp, "w", encoding=encoding, newline=newline) as handle:
            yield handle
        # Re-checked after the write: an export runs for minutes, which is long
        # enough for the destination to appear behind our back.
        if target.exists() and not overwrite:
            raise OutputExistsError(f"output file exists: {target}")
        os.replace(tmp, target)
    except BaseException:
        # Unlink failures are suppressed so the error that ended the export is
        # the one the caller sees.
        with suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise
