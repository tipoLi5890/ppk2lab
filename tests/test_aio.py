"""Async API over the simulator."""

import asyncio

from ppk2lab.aio import AsyncPPK2
from ppk2lab.protocol.samples import SampleBlock
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
