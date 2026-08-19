"""Asynchronous API for streaming and agent orchestration.

A thin, honest wrapper: blocking driver calls run in worker threads via
``asyncio.to_thread``; the stream is exposed as an async iterator. One
``AsyncPPK2`` must not be used from multiple coroutines concurrently.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import AsyncIterator
from typing import Any

from .capture.runner import CaptureResult
from .device import PPK2, StreamIterator
from .errors import UsageError
from .protocol.metadata import Metadata
from .protocol.samples import GapEvent, SampleBlock
from .types import Mode, StateChange

_SENTINEL = object()


def _quiet_close(device: PPK2) -> None:
    """Close a device nobody is waiting on; failures have no audience."""
    with contextlib.suppress(Exception):
        device.close()


def _close_abandoned_device(task: asyncio.Future[PPK2]) -> None:
    """Close a device whose opener was cancelled after the port had opened.

    ``asyncio.to_thread`` cannot interrupt its worker, so a cancelled
    ``AsyncPPK2.open()`` still ends with a real serial port claimed by this
    process and no reference left to close it — the next open on the same
    unit then fails with a busy port. The close runs on a daemon thread
    because it does serial I/O and may well outlive the event loop that was
    being torn down when the cancellation arrived.
    """
    if task.cancelled() or task.exception() is not None:
        return
    threading.Thread(
        target=_quiet_close,
        args=(task.result(),),
        name="ppk2lab-abandoned-close",
        daemon=True,
    ).start()


class AsyncStreamIterator(AsyncIterator[SampleBlock | GapEvent]):
    """Closable async iterator over one device's stream events.

    The async mirror of :class:`~ppk2lab.device.StreamIterator`, and it is a
    plain object rather than an async generator for one measured reason: an
    ``async for`` abandoned by an exception — or by ``task.cancel()``, which
    is what a test harness actually does — does not reach an async
    generator's ``finally`` until the event loop finalizes the generator.
    Until then the device stays claimed *and the PPK2 stays measuring*. With
    an explicit object the release is the caller's to make, promptly::

        async with adev.stream(duration_s=1.0) as events:
            async for event in events:
                ...

    ``async for event in adev.stream(...)`` still works unchanged, and
    ``contextlib.aclosing()`` still drives :meth:`aclose`.
    """

    def __init__(self, iterator: StreamIterator) -> None:
        self._iterator = iterator
        self._step: asyncio.Future[Any] | None = None
        self._awaiting = False
        self._closed = False

    def __aiter__(self) -> AsyncStreamIterator:
        return self

    async def __anext__(self) -> SampleBlock | GapEvent:
        if self._closed:
            raise StopAsyncIteration
        if self._awaiting:
            raise UsageError(
                "this stream is already being awaited by another coroutine",
                remediation="One device owns one serial channel: two readers would "
                "interleave on a 4-byte framing stream. Iterate the stream from a "
                "single coroutine, or fan the events out yourself.",
            )
        step = self._step
        if step is None:
            # The pending step lives on the instance, not in this frame, so a
            # cancelled await does not throw away the event a worker thread is
            # already fetching: the next __anext__ awaits the same step. A
            # discarded step would be a silent hole in the sample timeline —
            # exactly the loss this project reports rather than hides.
            step = asyncio.ensure_future(asyncio.to_thread(next, self._iterator, _SENTINEL))
            self._step = step
        self._awaiting = True
        try:
            # Shielded: the worker thread owns the serial channel and cannot
            # be interrupted, so cancelling the caller must not mark the step
            # dead while it still runs. Closing the generator underneath it
            # would raise "generator already executing" and leave the
            # generator alive to clear a device claim that may by then belong
            # to a later stream.
            event = await asyncio.shield(step)
        except Exception:
            # The step is spent and its failure is being delivered here.
            # CancelledError is not an Exception and deliberately does not
            # reach this clause: a cancelled await leaves the step on the
            # instance, still running or already holding an event, so the
            # next __anext__ receives it instead of skipping it.
            self._step = None
            raise
        else:
            self._step = None
        finally:
            self._awaiting = False
        if event is _SENTINEL:
            await self.aclose()
            raise StopAsyncIteration
        assert isinstance(event, SampleBlock | GapEvent)
        return event

    async def aclose(self) -> None:
        """Stop measuring and release the device claim. Idempotent."""
        if self._closed:
            return
        step = self._step
        if step is not None:
            if not step.done():
                try:
                    # A worker thread is inside next(). Wait for it rather
                    # than close under it; the wait is bounded by the stream's
                    # own idle and wall budgets, which are always armed.
                    await asyncio.shield(step)
                except asyncio.CancelledError:
                    # Cancelled a second time while reaping. Leave the object
                    # unclosed so a retried aclose() can still finish the job,
                    # and leave the stream to end on its own budget — its
                    # generator's finally stops the measurement and gives the
                    # claim back.
                    raise
                except Exception:
                    # The step's own failure has no consumer left; tearing the
                    # stream down is what is still owed to the device.
                    pass
            # Retrieve whatever the step produced — an event nobody will now
            # receive, or a failure with no consumer — so a discarded step
            # cannot resurface as an "exception was never retrieved" report
            # that names no owner.
            with contextlib.suppress(BaseException):
                step.exception()
        self._step = None
        self._closed = True
        await asyncio.to_thread(self._iterator.close)

    async def __aenter__(self) -> AsyncStreamIterator:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()


class AsyncPPK2:
    def __init__(self, device: PPK2) -> None:
        self.device = device

    @classmethod
    async def open(cls, **kwargs: Any) -> AsyncPPK2:
        opening: asyncio.Future[PPK2] = asyncio.ensure_future(
            asyncio.to_thread(lambda: PPK2.open(**kwargs))
        )
        try:
            device = await asyncio.shield(opening)
        except asyncio.CancelledError:
            # The worker keeps opening the port regardless; hand the result to
            # a cleanup that closes it instead of dropping it on the floor.
            opening.add_done_callback(_close_abandoned_device)
            raise
        return cls(device)

    async def close(self, *, restore_power: bool = True) -> list[StateChange]:
        return await asyncio.to_thread(lambda: self.device.close(restore_power=restore_power))

    async def __aenter__(self) -> AsyncPPK2:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # -- read-only ---------------------------------------------------------
    @property
    def info(self):
        return self.device.info

    @property
    def state(self):
        return self.device.state

    async def refresh_metadata(self) -> Metadata:
        return await asyncio.to_thread(self.device.refresh_metadata)

    # -- state-changing ----------------------------------------------------
    async def set_mode(self, mode: Mode, *, dry_run: bool = False) -> StateChange:
        return await asyncio.to_thread(lambda: self.device.set_mode(mode, dry_run=dry_run))

    async def set_source_voltage_mv(self, voltage_mv: int, *, dry_run: bool = False) -> StateChange:
        return await asyncio.to_thread(
            lambda: self.device.set_source_voltage_mv(voltage_mv, dry_run=dry_run)
        )

    async def set_dut_power(self, on: bool, *, dry_run: bool = False) -> StateChange:
        return await asyncio.to_thread(lambda: self.device.set_dut_power(on, dry_run=dry_run))

    async def reset(self, *, dry_run: bool = False) -> StateChange:
        return await asyncio.to_thread(lambda: self.device.reset(dry_run=dry_run))

    # -- measurement -------------------------------------------------------
    async def capture(self, **kwargs: Any) -> CaptureResult:
        """Run a capture in a worker thread.

        Cancelling the await does not stop the capture: ``run_capture`` has no
        cooperative stop and ``asyncio.to_thread`` cannot interrupt a worker,
        so the run continues to its own stopping condition and — with
        ``output=`` — still writes its artifact. Until it finishes, the device
        keeps the stream claim and further commands on this handle are
        refused. Bound a capture with ``duration_s``/``sample_limit`` rather
        than with a cancellation.
        """
        return await asyncio.to_thread(lambda: self.device.capture(**kwargs))

    def stream(self, **kwargs: Any) -> AsyncStreamIterator:
        """Async iterator over loss-aware stream events.

        Returns an :class:`AsyncStreamIterator`, which is an async context
        manager, and that is the documented idiom — a stream owns the
        instrument, so releasing it must not wait for the event loop to
        finalize an abandoned generator::

            async with adev.stream(duration_s=1.0) as events:
                async for event in events:
                    ...

        ``async for event in adev.stream(...)`` is unchanged, as is
        ``contextlib.aclosing()``. The device is claimed here rather than at
        the first iteration, matching :meth:`ppk2lab.device.PPK2.stream`;
        that claim costs no I/O, so it is safe to take on the event loop.
        """
        return AsyncStreamIterator(self.device.stream(**kwargs))
