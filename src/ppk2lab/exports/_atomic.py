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
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from io import TextIOWrapper
from pathlib import Path
from typing import Any

from ..errors import OutputExistsError, UsageError


def fsync_directory(directory: str | os.PathLike[str]) -> None:
    """Flush a directory entry to stable storage, where the OS supports it.

    Windows has no directory handle to sync and raises; that is not a failure
    worth ending an otherwise complete write over, so it is suppressed rather
    than branched on by platform.
    """
    with suppress(OSError, AttributeError):
        fd = os.open(directory, getattr(os, "O_DIRECTORY", os.O_RDONLY))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


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

    The bytes are fsynced before the rename and the parent directory after it.
    ``os.replace`` is atomic for the directory entry only, so without those the
    name can reach stable storage ahead of the data: after a power loss the
    file exists at full apparent length with a partly-zeroed tail, which reads
    like a complete export and fails no check this module makes.
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
            handle.flush()
            os.fsync(handle.fileno())
        # Re-checked after the write: an export runs for minutes, which is long
        # enough for the destination to appear behind our back.
        if target.exists() and not overwrite:
            raise OutputExistsError(f"output file exists: {target}")
        os.replace(tmp, target)
        fsync_directory(target.parent)
    except BaseException:
        # Unlink failures are suppressed so the error that ended the export is
        # the one the caller sees.
        with suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise


def write_comments(handle: Any, comments: Sequence[str] | None) -> None:
    """Write ``# ``-prefixed provenance lines before a CSV body.

    A derived file usually outlives the conversation that produced it, and a
    reader who finds one wants to know which board, which build, which run.
    Refuses embedded newlines: a comment that silently became two lines, the
    second of which is not a comment, would corrupt the file it describes.

    Terminated with CRLF to match ``csv.writer``'s default dialect, which the
    exporters use under ``newline=""``. A bare LF here left a file whose
    comment lines and data rows ended differently, and an RFC 4180 reader
    splitting on CRLF glued the whole preamble onto the header record.
    """
    # `str` satisfies `Sequence[str]` structurally, so a caller passing one
    # string gets it iterated character by character: `# s`, `# n`, `# :`.
    # mypy accepts that; only a runtime check catches it, and it has to come
    # before the loop because the elements of a str are themselves str.
    if isinstance(comments, str | bytes):
        raise UsageError(
            "comments must be a sequence of strings, not one string",
            remediation='Pass a list such as ["sn: POD01"], not "sn: POD01".',
        )
    for comment in comments or []:
        if not isinstance(comment, str):
            raise UsageError(
                f"a CSV comment must be a string, got {type(comment).__name__}",
                remediation="Convert the value yourself, so the recorded text is the "
                "text you meant.",
            )
        if "\n" in comment or "\r" in comment:
            raise UsageError(
                "a CSV comment cannot contain a newline",
                remediation="Pass one string per line.",
            )
        handle.write(f"# {comment}\r\n")
