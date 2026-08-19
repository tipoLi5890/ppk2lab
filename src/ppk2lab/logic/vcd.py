"""VCD export of D0-D7 for PulseView/GTKWave.

Timescale is the native 10 us sample period; timestamps are timeline sample
indexes. During sample gaps every exported channel is driven to ``x`` so
viewers cannot mistake missing data for a constant level.
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
) -> int:
    """Write a VCD file; returns the number of value-change records."""
    channels = channels if channels is not None else list(range(8))
    ids = {ch: chr(33 + i) for i, ch in enumerate(channels)}

    own = False
    if isinstance(path, io.TextIOBase):
        fh = path
    else:
        fh = open(path, "w", encoding="ascii")  # noqa: SIM115 - closed in finally
        own = True
    records = 0
    try:
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

        def emit(time: int, ch: int, value: str) -> None:
            nonlocal records
            fh.write(f"#{time}\n{value}{ids[ch]}\n")
            records += 1

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
    finally:
        if own:
            fh.close()
    return records
