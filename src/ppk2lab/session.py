"""Streaming session: reader thread, bounded queue, loss-aware event stream.

The reader thread pulls raw bytes from the transport into a bounded queue.
If the consumer falls behind and the queue fills, whole chunks are dropped
and the exact dropped byte count is reported to the parser, which converts
it into an explicit ``GapEvent`` — data loss is never silent.
"""

from __future__ import annotations

import contextlib
import queue
import threading
import time
from collections.abc import Iterator

from .errors import StreamStalledError, TransportError
from .protocol.samples import GapEvent, SampleBlock, SampleStreamParser
from .transport.base import Transport

_SENTINEL = object()

#: Maximum silence tolerated between two byte deliveries while streaming.
#: The device sends ~400 kB/s continuously, so seconds of silence means it
#: stopped — a state that produces no serial error on its own.
DEFAULT_IDLE_TIMEOUT_S = 5.0


class StreamSession:
    """Iterates ``SampleBlock`` / ``GapEvent`` items from a measuring device."""

    def __init__(
        self,
        transport: Transport,
        *,
        read_chunk: int = 16384,
        queue_chunks: int = 256,
        read_timeout_s: float = 0.05,
        start_index: int = 0,
    ) -> None:
        self.transport = transport
        self.read_chunk = read_chunk
        self.read_timeout_s = read_timeout_s
        self.parser = SampleStreamParser(start_index=start_index)
        self._queue: queue.Queue = queue.Queue(maxsize=queue_chunks)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pending_dropped = 0
        self.dropped_bytes_total = 0
        self.error: Exception | None = None

    # -- reader thread -----------------------------------------------------
    def _reader(self) -> None:
        try:
            while not self._stop.is_set():
                data = self.transport.read(self.read_chunk, self.read_timeout_s)
                if not data:
                    continue
                if self._pending_dropped:
                    try:
                        self._queue.put_nowait(("dropped", self._pending_dropped))
                        self._pending_dropped = 0
                    except queue.Full:
                        self._pending_dropped += len(data)
                        self.dropped_bytes_total += len(data)
                        continue
                try:
                    self._queue.put_nowait(("data", data))
                except queue.Full:
                    self._pending_dropped += len(data)
                    self.dropped_bytes_total += len(data)
        except Exception as exc:
            self.error = exc
            with contextlib.suppress(queue.Full):
                self._queue.put_nowait(("error", exc))
        finally:
            with contextlib.suppress(queue.Full):
                self._queue.put_nowait(("end", _SENTINEL))

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._reader, name="ppk2lab-reader", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    # -- consumer ----------------------------------------------------------
    def events(
        self,
        *,
        sample_limit: int | None = None,
        wall_timeout_s: float | None = None,
        idle_timeout_s: float | None = DEFAULT_IDLE_TIMEOUT_S,
    ) -> Iterator[SampleBlock | GapEvent]:
        """Yield parsed events until ``sample_limit`` timeline samples passed.

        ``sample_limit`` counts timeline positions (stored samples plus known
        missing samples), so a gappy capture still ends near the requested
        duration instead of stretching to fill it.

        Two independent budgets bound the wait, because a PPK2 that stops
        streaming while keeping its serial port open produces no error at all:
        ``wall_timeout_s`` caps the whole run, and ``idle_timeout_s`` caps the
        silence between two consecutive byte deliveries. Whichever trips
        first raises :class:`~ppk2lab.errors.TransportError`; data already
        received is preserved by the caller.
        """
        started = time.monotonic()
        deadline = started + wall_timeout_s if wall_timeout_s else None
        last_data = started
        while True:
            if sample_limit is not None and self.parser.next_index >= sample_limit:
                return
            now = time.monotonic()
            if deadline is not None and now > deadline:
                raise StreamStalledError(
                    "stream stalled: wall-clock timeout before reaching the requested sample count",
                    remediation="Check the USB connection and system load, then retry the "
                    "capture. Partial data was preserved with loss markers.",
                )
            if idle_timeout_s is not None and now - last_data > idle_timeout_s:
                raise StreamStalledError(
                    f"stream stalled: no data from the device for {idle_timeout_s:g} s "
                    "while the port stayed open",
                    remediation="The device stopped streaming without closing the port. "
                    "Reconnect it and run `ppk2lab doctor --json`. Partial data was "
                    "preserved with an interruption record.",
                )
            try:
                kind, payload = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            last_data = time.monotonic()
            if kind == "data":
                for event in self.parser.feed(payload):
                    yield event
                    if (
                        sample_limit is not None
                        and isinstance(event, SampleBlock)
                        and event.end_index >= sample_limit
                    ):
                        return
            elif kind == "dropped":
                yield self.parser.notify_dropped_bytes(payload)
            elif kind == "error":
                raise TransportError(
                    f"stream interrupted: {payload}",
                    remediation="The device stopped responding (USB unplug or I/O error). "
                    "Partial data was preserved with an interruption record.",
                ) from payload
            elif kind == "end":
                return

    def __enter__(self) -> StreamSession:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
