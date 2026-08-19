"""Current/logic profiles consumed by the PPK2 simulator."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from .signals import merge_logic, spi_wave, uart_wave


@runtime_checkable
class Profile(Protocol):
    """Maps a timeline sample index to (current_ua, logic_byte)."""

    def sample(self, index: int) -> tuple[float, int]: ...


class ConstantProfile:
    def __init__(self, current_ua: float, logic: int = 0) -> None:
        self.current_ua = current_ua
        self.logic = logic

    def sample(self, index: int) -> tuple[float, int]:
        return self.current_ua, self.logic


class StepProfile:
    """Piecewise-constant profile: list of (sample_count, current_ua, logic)."""

    def __init__(
        self,
        steps: Sequence[tuple[int, float, int]],
        *,
        repeat: bool = False,
        default: tuple[float, int] = (0.0, 0),
    ) -> None:
        self.steps = list(steps)
        self.repeat = repeat
        self.default = default
        self.total = sum(count for count, _, _ in self.steps)

    def sample(self, index: int) -> tuple[float, int]:
        if self.total == 0:
            return self.default
        if self.repeat:
            index %= self.total
        elif index >= self.total:
            return self.default
        for count, current, logic in self.steps:
            if index < count:
                return current, logic
            index -= count
        return self.default


class ArrayProfile:
    """Profile backed by explicit per-sample arrays."""

    def __init__(
        self,
        currents_ua: Sequence[float] | float,
        logic: Sequence[int] | bytes | int = 0,
        *,
        repeat: bool = False,
        default_current_ua: float = 0.0,
        default_logic: int = 0,
    ) -> None:
        self.currents = currents_ua
        self.logic = logic
        self.repeat = repeat
        self.default_current_ua = default_current_ua
        self.default_logic = default_logic
        lengths = []
        if not isinstance(currents_ua, int | float):
            lengths.append(len(currents_ua))
        if not isinstance(logic, int):
            lengths.append(len(logic))
        self.length = max(lengths) if lengths else 0

    def sample(self, index: int) -> tuple[float, int]:
        if self.repeat and self.length:
            index %= self.length
        if isinstance(self.currents, int | float):
            current = float(self.currents)
        elif index < len(self.currents):
            current = float(self.currents[index])
        else:
            current = self.default_current_ua
        if isinstance(self.logic, int):
            logic = self.logic
        elif index < len(self.logic):
            logic = int(self.logic[index])
        else:
            logic = self.default_logic
        return current, logic


class DemoActivityProfile:
    """The profile behind ``--simulate``: a repeating 100 ms activity cycle.

    Per cycle (10 000 samples):

    - samples 0-1499: active burst at 12 mA, D1 high as a busy marker, and one
      SPI mode-0 transaction (0x9F, 0x00) at 5 kHz on D3=SCLK/D4=MOSI/D5=MISO/D6=CS;
    - samples 1500-~2340: UART TX of ``b"TX_DONE\\n"`` at 9600 baud on D0 while
      drawing 25 uA;
    - remainder: 6 uA sleep;
    - D2 carries a continuous 1 kHz square wave.

    This makes every README quickstart command (capture, decode, measure,
    assert) reproducible without hardware. Simulated data is for testing the
    toolchain, never a substitute for real measurements.
    """

    CYCLE_SAMPLES = 10_000
    SLEEP_UA = 6.0
    UART_UA = 25.0
    BURST_UA = 12_000.0

    def __init__(self) -> None:
        cycle = self.CYCLE_SAMPLES
        currents = [self.SLEEP_UA] * cycle
        for i in range(0, 1500):
            currents[i] = self.BURST_UA

        uart = uart_wave(b"TX_DONE\n", 9600, idle_before=0, idle_after=0)
        d0 = [1] * cycle
        for i, level in enumerate(uart):
            if 1500 + i < cycle:
                d0[1500 + i] = level
                currents[1500 + i] = self.UART_UA

        d1 = [1 if i < 1500 else 0 for i in range(cycle)]

        d2 = [(i // 50) % 2 for i in range(cycle)]  # 1 kHz square at 100 kS/s

        spi = spi_wave([0x9F, 0x00], [0x00, 0x52], clock_hz=5000, mode=0, idle_before=0)
        offset = 200

        def placed(wave: list[int], idle: int) -> list[int]:
            out = [idle] * cycle
            for i, level in enumerate(wave):
                if offset + i < cycle:
                    out[offset + i] = level
            return out

        d3 = placed(spi["sclk"], 0)
        d4 = placed(spi["mosi"], 0)
        d5 = placed(spi["miso"], 0)
        d6 = placed(spi["cs"], 1)

        logic = merge_logic(
            {0: d0, 1: d1, 2: d2, 3: d3, 4: d4, 5: d5, 6: d6},
            length=cycle,
            idle_levels={0: 1, 6: 1},
        )
        self._profile = ArrayProfile(currents, logic, repeat=True)

    def sample(self, index: int) -> tuple[float, int]:
        return self._profile.sample(index)
