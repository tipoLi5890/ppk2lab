"""Connection registry and fan-out.

The supervisor thread must never block on a browser. It hands every message to
:meth:`Hub.publish`, which hops to the event loop and drops it into per-
connection queues without awaiting anything, so a browser that has stopped
reading slows nobody down.

Each connection has two queues, and the difference between them is the whole
back-pressure policy:

* **control** is JSON -- state changes, the session log, replies. It is never
  dropped. A connection that cannot absorb 256 of these is not a connection,
  and it is closed rather than allowed to fall silently out of date.
* **data** is bucket frames. On overflow the queue is emptied and the console
  is *told*, on the control queue, exactly which stretch of the timeline it
  will never receive. The console then breaks its trace there and counts it
  separately from instrument loss, because "your browser did not receive it"
  and "the instrument never delivered it" are different facts and drawing them
  the same way would let a slow link look like a lossy instrument.

One more thing lives here: the control lease. Every viewer sees everything --
an instrument shows the same reading to everyone in the room -- but only one
connection at a time may command the device, so two operators cannot interleave
contradictory changes.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
from typing import TYPE_CHECKING, Any

from . import protocol

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterable

#: JSON backlog before a connection is considered unable to keep up. State
#: messages are human-speed; 256 is minutes of them.
CTRL_QUEUE = 256

#: Bucket frames held for a connection that is behind. At roughly 20 frames a
#: second this is about five seconds of stream.
DATA_QUEUE = 100


class Connection:
    """One attached console."""

    def __init__(self, cid: str) -> None:
        self.id = cid
        #: Unguessable per connection. It is the CSRF secret a cross-origin page
        #: cannot know, the identity of the control lease, and the attribution
        #: in the session log -- one value doing three jobs that all need the
        #: same property.
        self.control_token = secrets.token_urlsafe(24)
        self.ctrl: asyncio.Queue[Any] = asyncio.Queue(maxsize=CTRL_QUEUE)
        self.data: asyncio.Queue[bytes] = asyncio.Queue(maxsize=DATA_QUEUE)
        self.closed = asyncio.Event()
        #: Timeline extent this connection was not sent, pending a desync note.
        self._dropped_from: int | None = None
        self._dropped_to: int | None = None
        self._dropped_frames = 0

    def offer_control(self, message: Any) -> bool:
        """Queue a control message. False means this connection is hopeless."""
        try:
            self.ctrl.put_nowait(message)
        except asyncio.QueueFull:
            return False
        return True

    def offer_data(self, frame: bytes) -> None:
        """Queue a sample frame, discarding rather than buffering without end."""
        if self._try_put(frame):
            return
        kept = self._discard_backlog()
        # The distribution is absolute state, not a stretch of timeline, so
        # there is nothing to report about having dropped it and nothing gained
        # by doing so -- one grid is 1.4 kB against a bucket stream that
        # overflowed a hundred frames. Dropping it anyway is what made the
        # panel freeze: under sustained back-pressure every periodic grid went
        # into a discard, so "absolute values heal on the next frame" was true
        # of every frame except the ones that never arrived. The newest survives
        # the discard; the console assigns rather than accumulates, so a stale
        # one landing before a newer one is harmless.
        if kept is not None:
            self._try_put(kept)
        self._try_put(frame)

    def _try_put(self, frame: bytes) -> bool:
        try:
            self.data.put_nowait(frame)
        except asyncio.QueueFull:
            return False
        return True

    def _discard_backlog(self) -> bytes | None:
        """Empty the queue, returning the newest frame worth keeping."""
        kept: bytes | None = None
        while True:
            try:
                frame = self.data.get_nowait()
            except asyncio.QueueEmpty:
                break
            if frame and frame[0] == protocol.TAG_HISTOGRAM:
                kept = frame
                continue
            self._note_discarded(frame)
        return kept

    def _note_discarded(self, frame: bytes) -> None:
        if not frame or frame[0] != protocol.TAG_BUCKETS:
            return
        try:
            batch = protocol.unpack_buckets(frame)
        except ValueError:  # pragma: no cover - we packed it ourselves
            return
        first = batch["first_index"]
        last = batch["start_index"][-1] + batch["n"][-1] + batch["gap"][-1] + batch["excluded"][-1]
        self._dropped_from = first if self._dropped_from is None else self._dropped_from
        self._dropped_to = last
        self._dropped_frames += 1

    def take_desync(self) -> dict[str, Any] | None:
        """The gap this connection has not yet been told about, once."""
        if self._dropped_from is None or self._dropped_to is None:
            return None
        message = protocol.desync(
            from_index=self._dropped_from,
            to_index=self._dropped_to,
            buckets_dropped=self._dropped_frames,
        )
        self._dropped_from = self._dropped_to = None
        self._dropped_frames = 0
        return message


class Hub:
    """Every attached console, and which one may command the device."""

    def __init__(self) -> None:
        self._connections: dict[str, Connection] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lease: str | None = None
        self._next_id = 0

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    # -- registry ---------------------------------------------------------
    def attach(self) -> Connection:
        self._next_id += 1
        connection = Connection(f"c{self._next_id}")
        self._connections[connection.id] = connection
        return connection

    def detach(self, connection: Connection) -> None:
        self._connections.pop(connection.id, None)
        connection.closed.set()
        if self._lease == connection.id:
            # A console that goes away releases the instrument. Anything else
            # would need a timeout to recover from a closed laptop lid.
            self._lease = None

    def __len__(self) -> int:
        return len(self._connections)

    def connections(self) -> Iterable[Connection]:
        return list(self._connections.values())

    # -- the control lease ------------------------------------------------
    @property
    def lease_holder(self) -> str | None:
        return self._lease

    def claim_control(self, connection: Connection) -> bool:
        """Take the lease if it is free, or confirm this connection holds it."""
        if self._lease in (None, connection.id):
            self._lease = connection.id
            return True
        return False

    def release_control(self, connection: Connection) -> None:
        if self._lease == connection.id:
            self._lease = None

    # -- fan-out ----------------------------------------------------------
    def publish(self, message: Any, target: str | None = None) -> None:
        """Called from the supervisor thread. Never blocks, never awaits."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        # The loop can close between the check above and this call.
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(self._fan_out, message, target)

    def _fan_out(self, message: Any, target: str | None) -> None:
        if target is not None:
            connection = self._connections.get(target)
            targets = [connection] if connection else []
        else:
            targets = list(self._connections.values())
        for connection in targets:
            if isinstance(message, bytes):
                connection.offer_data(message)
            elif not connection.offer_control(message):
                # It cannot keep up with human-speed messages. Close it rather
                # than let it show state that quietly stopped being true.
                connection.closed.set()
