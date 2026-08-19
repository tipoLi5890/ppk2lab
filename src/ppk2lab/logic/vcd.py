"""VCD export of D0-D7 for PulseView/GTKWave.

Timescale is the native 10 us sample period; timestamps are timeline sample
indexes. During sample gaps every exported channel is driven to ``x`` so
viewers cannot mistake missing data for a constant level.

When given a path the file is written to a temp path and moved into place, so
a failed export never replaces a previous one. When given an open stream the
caller owns it and nothing is renamed.
"""

from __future__ import annotations

import io
import os
from collections.abc import Iterable

from .._version import __version__
from ..protocol.samples import GapEvent, SampleBlock


def export_vcd(
    events: Iterable[SampleBlock | GapEvent],
    path: str | os.PathLike[str] | io.TextIOBase,
    channels: list[int] | None = None,
    *,
    comment: str | None = None,
    overwrite: bool = True,
) -> int:
    """Write a VCD file; returns the number of value-change records.

    ``overwrite`` applies only when *path* is a filesystem path. It defaults to
    True because the CLI refuses an existing ``--output`` before calling; pass
    False to make the refusal this writer's job.
    """
    channels = channels if channels is not None else list(range(8))
    if isinstance(path, io.TextIOBase):
        return _write_vcd(path, events, channels, comment)
    # Imported here rather than at module level: ppk2lab.exports re-exports
    # this function, so the module-level form would close an import cycle.
    from ..exports._atomic import atomic_write

    with atomic_write(path, overwrite=overwrite, encoding="ascii") as fh:
        return _write_vcd(fh, events, channels, comment)


def _write_vcd(
    fh: io.TextIOBase,
    events: Iterable[SampleBlock | GapEvent],
    channels: list[int],
    comment: str | None,
) -> int:
    ids = {ch: chr(33 + i) for i, ch in enumerate(channels)}
    records = 0

    fh.write(f"$version ppk2lab {__version__} $end\n")
    if comment:
        fh.write(f"$comment {comment} $end\n")
    fh.write("$timescale 10 us $end\n")
    fh.write("$scope module ppk2 $end\n")
    for ch in channels:
        fh.write(f"$var wire 1 {ids[ch]} D{ch} $end\n")
    fh.write("$upscope $end\n$enddefinitions $end\n")

    prev: dict[int, str] = dict.fromkeys(channels, "x")
    fh.write("$dumpvars\n")
    for ch in channels:
        fh.write(f"x{ids[ch]}\n")
    fh.write("$end\n")

    def emit_at(time: int, lines: list[str]) -> None:
        nonlocal records
        if not lines:
            return
        fh.write(f"#{time}\n")
        for line in lines:
            fh.write(line + "\n")
        records += len(lines)

    for event in events:
        if isinstance(event, GapEvent):
            lines = []
            for ch in channels:
                if prev[ch] != "x":
                    prev[ch] = "x"
                    lines.append(f"x{ids[ch]}")
            emit_at(event.index, lines)
            continue
        logic = event.logic
        start = event.start_index
        for offset, byte in enumerate(logic):
            lines = []
            for ch in channels:
                value = "1" if byte & (1 << ch) else "0"
                if value != prev[ch]:
                    prev[ch] = value
                    lines.append(f"{value}{ids[ch]}")
            if lines:
                emit_at(start + offset, lines)
    return records
