"""Asynchronous API for streaming and agent orchestration.

A thin, honest wrapper: blocking driver calls run in worker threads via
``asyncio.to_thread``; the stream is exposed as an async iterator. One
``AsyncPPK2`` must not be used from multiple coroutines concurrently.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from .capture.runner import CaptureResult
from .device import PPK2
from .protocol.metadata import Metadata
from .protocol.samples import GapEvent, SampleBlock
from .types import Mode, StateChange

_SENTINEL = object()


class AsyncPPK2:
    def __init__(self, device: PPK2) -> None:
        self.device = device

    @classmethod
    async def open(cls, **kwargs: Any) -> AsyncPPK2:
        device = await asyncio.to_thread(lambda: PPK2.open(**kwargs))
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
        return await asyncio.to_thread(lambda: self.device.capture(**kwargs))

    async def stream(self, **kwargs: Any) -> AsyncIterator[SampleBlock | GapEvent]:
        """Async iterator over loss-aware stream events."""
        iterator: Any = self.device.stream(**kwargs)
        try:
            while True:
                event = await asyncio.to_thread(next, iterator, _SENTINEL)
                if event is _SENTINEL:
                    return
                assert isinstance(event, SampleBlock | GapEvent)
                yield event
        finally:
            await asyncio.to_thread(iterator.close)
