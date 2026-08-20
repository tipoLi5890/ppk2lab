"""Streaming session: reader thread, bounded queue, loss-aware event stream.

The reader thread pulls raw bytes from the transport into a bounded queue.
If the consumer falls behind and the queue fills, whole chunks are dropped
and the exact dropped byte count is reported to the parser, which converts
it into an explicit ``GapEvent`` — data loss is never silent.

The queue is bounded in **bytes**, not in items. A read returns
``1 + in_waiting`` bytes, and ``in_waiting`` is small precisely when the
reader is keeping up: on one macOS host a 30 s capture averaged 77 bytes per
read. An item-count bound therefore sized the buffer by how well the reader
was doing rather than by memory, and 256 items of 77 bytes is 20 kB — 50 ms
of stream, not the 4 MB the read size suggests. Any consumer pause longer
than that dropped samples.
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
        queue_bytes: int = 4 * 1024 * 1024,
        read_timeout_s: float = 0.05,
        start_index: int = 0,
    ) -> None:
        self.transport = transport
        self.read_chunk = read_chunk
        self.queue_bytes = queue_bytes
        self.read_timeout_s = read_timeout_s
        self.parser = SampleStreamParser(start_index=start_index)
        # Unbounded in items, bounded in bytes by _queued_bytes below: the
        # item count says nothing about how much stream is buffered.
        self._queue: queue.Queue = queue.Queue()
        self._budget = threading.Lock()
        self._queued_bytes = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pending_dropped = 0
        self.dropped_bytes_total = 0
        #: Largest number of bytes ever queued at once; a run that never
        #: approached ``queue_bytes`` had a consumer that kept up.
        self.peak_queued_bytes = 0
        self.error: Exception | None = None

    # -- reader thread -----------------------------------------------------
    def _reader(self) -> None:
        try:
            while not self._stop.is_set():
                data = self.transport.read(self.read_chunk, self.read_timeout_s)
                if not data:
                    continue
                if not self._reserve(len(data)):
                    # No room: this read is lost. Remember the exact count so
                    # the parser can turn it into an explicit gap once the
                    # consumer catches up.
                    self._pending_dropped += len(data)
                    self.dropped_bytes_total += len(data)
                    continue
                if self._pending_dropped:
                    # The marker is bookkeeping, not stream bytes, so it is
                    # never itself subject to the budget.
                    self._queue.put_nowait(("dropped", self._pending_dropped))
                    self._pending_dropped = 0
                self._queue.put_nowait(("data", data))
        except Exception as exc:
            self.error = exc
            with contextlib.suppress(queue.Full):
                self._queue.put_nowait(("error", exc))
        finally:
            with contextlib.suppress(queue.Full):
                if self._pending_dropped:
                    # A drop only rides along with the next successful read, so
                    # a stream that ends while still overflowing — a capture
                    # stopped mid-loss, an unplugged device — would take its
                    # last gap to the grave, leaving dropped_bytes_total larger
                    # than every gap the timeline shows. Loss is never silent,
                    # including the loss that happened last.
                    self._queue.put_nowait(("dropped", self._pending_dropped))
                    self._pending_dropped = 0
                self._queue.put_nowait(("end", _SENTINEL))

    def _reserve(self, n: int) -> bool:
        """Claim ``n`` bytes of queue budget; False when the buffer is full.

        An empty queue always admits, whatever the read's size. Without that,
        a read larger than the whole budget is refused on every iteration
        forever: the consumer sees no events, the parser is never told (the
        pending-drop marker only rides along with a successful reserve), and
        the idle timeout blames the device for going quiet while the host is
        discarding 100% of the stream. The budget is a back-pressure target,
        and one read past it beats reporting a host fault as an instrument
        fault.
        """
        with self._budget:
            if self._queued_bytes and self._queued_bytes + n > self.queue_bytes:
                return False
            self._queued_bytes += n
            if self._queued_bytes > self.peak_queued_bytes:
                self.peak_queued_bytes = self._queued_bytes
            return True

    def _release(self, n: int) -> None:
        with self._budget:
            self._queued_bytes -= n

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
            if self.error is not None and self._queue.empty():
                # The reader failed and its sentinel may have been dropped
                # when the queue was full. Surfacing a stall instead would
                # blame the device for going quiet when it was unplugged.
                raise TransportError(
                    f"stream interrupted: {self.error}",
                    remediation="The device stopped responding (USB unplug or I/O error). "
                    "Partial data was preserved with an interruption record.",
                ) from self.error
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
            # Stamp after the consumer returns, further down, so a slow
            # consumer is never mistaken for a silent device.
            if kind == "data":
                self._release(len(payload))
                for event in self.parser.feed(payload):
                    yield event
                    if (
                        sample_limit is not None
                        and isinstance(event, SampleBlock)
                        and event.end_index >= sample_limit
                    ):
                        return
                last_data = time.monotonic()
            elif kind == "dropped":
                yield self.parser.notify_dropped_bytes(payload)
                last_data = time.monotonic()
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
