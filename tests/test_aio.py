"""Async API over the simulator."""

import asyncio
import contextlib
import gc
import itertools
import time

import pytest

from ppk2lab.aio import AsyncPPK2
from ppk2lab.errors import UsageError
from ppk2lab.protocol.samples import GapEvent, SampleBlock
from ppk2lab.testing.profiles import ConstantProfile
from ppk2lab.transport.mock import MockTransport, SimulatedPPK2
from ppk2lab.types import Mode


async def _open():
    simulator = SimulatedPPK2(profile=ConstantProfile(500.0))
    device = await asyncio.to_thread(
        lambda: __import__("ppk2lab.device", fromlist=["PPK2"]).PPK2.open(
            transport=MockTransport(simulator), simulate=True
        )
    )
    return AsyncPPK2(device)


def test_async_capture_and_state_change():
    async def scenario():
        adev = await _open()
        async with adev:
            change = await adev.set_mode(Mode.SOURCE)
            assert change.applied
            result = await adev.capture(duration_s=0.02)
            assert result.complete
            assert result.stats.stored_samples == 2000
        return True

    assert asyncio.run(scenario())


def test_async_stream_iterator():
    async def scenario():
        adev = await _open()
        total = 0
        async with adev:
            async for event in adev.stream(sample_limit=1500):
                if isinstance(event, SampleBlock):
                    total += len(event)
        return total

    assert asyncio.run(scenario()) >= 1500


def test_aclosing_releases_the_device_when_the_body_raises():
    """An abandoned `async for` leaves the underlying async generator to the
    event loop's finalizer, so `aclosing` is what makes release prompt — the
    async counterpart of `with device.stream(...)`."""

    async def scenario():
        adev = await _open()
        async with adev:
            with pytest.raises(RuntimeError):
                async with contextlib.aclosing(adev.stream(sample_limit=1_000_000)) as events:
                    async for _event in events:
                        raise RuntimeError("consumer failed")
            assert adev.device._stream_active is False
            assert adev.state.measuring is False
            result = await adev.capture(sample_limit=500)
            return result.capture.stored_count

    assert asyncio.run(scenario()) >= 500


# -- async stream lifecycle ------------------------------------------------
def test_async_with_releases_the_device_when_the_body_raises():
    """`async with adev.stream(...) as events:` is the documented idiom, and
    it must release the instrument even when the body fails."""

    async def scenario():
        adev = await _open()
        async with adev:
            with pytest.raises(RuntimeError):
                async with adev.stream(sample_limit=1_000_000) as events:
                    async for _event in events:
                        raise RuntimeError("consumer failed")
            assert adev.device._stream_active is False
            assert adev.state.measuring is False
            result = await adev.capture(sample_limit=500)
            return result.capture.stored_count

    assert asyncio.run(scenario()) >= 500


def test_bare_async_for_releases_the_device_when_the_body_raises():
    """The plain `async for` form is frozen public surface, so abandoning it
    must give the device back — once nothing is holding the iterator.

    That qualifier is the whole test. `AsyncStreamIterator` is deliberately a
    plain object rather than an async generator, so Python calls nothing when
    the loop is abandoned; what returns the claim is `StreamIterator.__del__`.
    Whether that runs depends on what still references the iterator, and an
    abandoned `async for` can leave it reachable from the frame that ran the
    loop — which is exactly what `_require_no_active_stream` warns about when
    it says a named iterator held alive by a stored traceback keeps the device
    claimed.

    So the loop runs in a frame of its own, and the release is waited for
    rather than sampled at one instant. Two measured facts on Linux CI say
    why the wait is the honest assertion — both are properties of CPython's
    finalization, not of this library's contract:

    * the abandoned frame, its exception and its traceback form a cycle, so
      dropping the last reference needs a collection, and one `gc.collect()`
      is not guaranteed to be the collection that runs `__del__`; and
    * a collection runs on whichever thread allocated into a full generation,
      so `StreamIterator.__del__` was seen running on an `asyncio.to_thread`
      worker while this frame read the state — reading it mid-release.

    Sampled once, the assertion failed on 6-14% of runs across 3.11, 3.13 and
    3.14 alike; it is not a version quirk.
    """

    async def scenario():
        adev = await _open()
        async with adev:

            async def abandon():
                with pytest.raises(RuntimeError):
                    async for _event in adev.stream(sample_limit=1_000_000):
                        raise RuntimeError("consumer failed")

            await abandon()
            deadline = time.monotonic() + 5.0
            while True:
                gc.collect()
                released = not adev.device._stream_active and not adev.state.measuring
                if released or time.monotonic() >= deadline:
                    break
                # Yield: the release may be in flight on another thread.
                await asyncio.sleep(0.01)
            return adev.device._stream_active, adev.state.measuring

    assert asyncio.run(scenario()) == (False, False)


def test_cancelling_a_consumer_task_releases_the_device():
    """`task.cancel()` is what a test harness or a timeout actually does. The
    stream owns the instrument, so a cancelled consumer must leave the device
    released and not measuring, and the handle usable afterwards."""

    async def scenario():
        adev = await _open()
        async with adev:
            started = asyncio.Event()

            async def consume():
                async with adev.stream(sample_limit=1_000_000) as events:
                    async for _event in events:
                        started.set()
                        await asyncio.sleep(30)  # cancellation lands here

            task = asyncio.create_task(consume())
            await asyncio.wait_for(started.wait(), 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            assert adev.device._stream_active is False
            assert adev.state.measuring is False
            result = await asyncio.wait_for(adev.capture(sample_limit=500), 10)
            return result.capture.stored_count

    assert asyncio.run(scenario()) >= 500


def test_cancelling_an_in_flight_step_still_closes_cleanly():
    """Cancellation landing inside `__anext__` leaves a worker thread inside
    the underlying generator. Closing under it would raise "generator already
    executing", so aclose() waits for the step first."""

    async def scenario():
        adev = await _open()
        async with adev:
            events = adev.stream(sample_limit=1_000_000)
            await events.__anext__()  # prime: the stream is live
            step = asyncio.ensure_future(events.__anext__())
            await asyncio.sleep(0)  # let it reach the await, then cancel it
            step.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await step
            await events.aclose()
            await events.aclose()  # idempotent
            assert adev.device._stream_active is False
            assert adev.state.measuring is False
            return True

    assert asyncio.run(scenario())


def test_a_cancelled_await_does_not_drop_an_event():
    """A step already fetching an event outlives the await that was
    cancelled, so a retried `__anext__` receives it. Dropping it would be a
    silent hole in the sample timeline — the loss this project reports."""

    async def scenario():
        adev = await _open()
        async with adev:
            async with adev.stream(sample_limit=6000) as events:
                first = await events.__anext__()
                step = asyncio.ensure_future(events.__anext__())
                await asyncio.sleep(0)
                step.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await step
                seen = [first]
                async for event in events:
                    seen.append(event)
            return seen

    seen = asyncio.run(scenario())
    blocks = [e for e in seen if isinstance(e, SampleBlock)]
    assert not [e for e in seen if isinstance(e, GapEvent)]
    assert sum(len(b) for b in blocks) >= 6000
    # Contiguous timeline: the cancelled await skipped nothing.
    for previous, following in itertools.pairwise(blocks):
        assert following.start_index == previous.end_index


def test_two_coroutines_cannot_iterate_one_stream():
    """One device owns one serial channel; a second reader would interleave
    on a 4-byte framing stream and both would see the same event."""

    async def scenario():
        adev = await _open()
        async with adev:
            async with adev.stream(sample_limit=1_000_000) as events:
                first = asyncio.ensure_future(events.__anext__())
                await asyncio.sleep(0)  # first is now awaiting its step
                with pytest.raises(UsageError):
                    await events.__anext__()
                await first
            return True

    assert asyncio.run(scenario())


def test_a_second_stream_is_refused_when_it_is_asked_for():
    """The claim is taken by `stream()` itself, matching the sync API, so the
    refusal arrives when the caller asks rather than at the first `async for`
    step — by which point the mistake is much harder to locate."""

    async def scenario():
        adev = await _open()
        async with adev:
            async with adev.stream(sample_limit=1_000_000):
                with pytest.raises(UsageError):
                    adev.stream(sample_limit=100)
            return True

    assert asyncio.run(scenario())


def test_closing_the_device_releases_a_named_async_stream():
    """A named async iterator is held by whoever named it, so closing the
    device has to release it — while the transport is still open."""

    async def scenario():
        adev = await _open()
        events = adev.stream(sample_limit=1_000_000)
        await events.__anext__()
        assert adev.device._stream_active is True
        await adev.close()
        return adev.device._stream_active, adev.state.measuring

    assert asyncio.run(scenario()) == (False, False)


def test_iterating_a_closed_async_stream_stops():
    async def scenario():
        adev = await _open()
        async with adev:
            events = adev.stream(sample_limit=500)
            await events.aclose()
            return [event async for event in events]

    assert asyncio.run(scenario()) == []


def test_cancelling_open_closes_the_port_it_opened():
    """`asyncio.to_thread` cannot interrupt its worker, so a cancelled open()
    still ends with a real port claimed by this process. It must not be left
    to no one."""

    closed: list[bool] = []

    async def scenario():
        simulator = SimulatedPPK2(profile=ConstantProfile(500.0))
        transport = MockTransport(simulator)
        task = asyncio.ensure_future(AsyncPPK2.open(transport=transport, simulate=True))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # The abandoned handle is closed on a daemon thread; give it a moment.
        for _ in range(200):
            if not transport.is_open:
                break
            await asyncio.sleep(0.01)
        closed.append(not transport.is_open)

    asyncio.run(scenario())
    assert closed == [True]
