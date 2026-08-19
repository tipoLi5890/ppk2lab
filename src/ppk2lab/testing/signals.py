"""Sample-accurate UART and SPI waveform builders at the 100 kS/s grid.

Waves are expanded with fractional phase accumulation, so long sequences do
not drift relative to the ideal bit clock — exactly the situation a real
decoder faces at non-integer samples-per-bit ratios.
"""

from __future__ import annotations

from collections.abc import Sequence

from ..errors import UsageError
from ..types import SAMPLE_RATE_HZ

Parity = str | None  # "even" | "odd" | None


def uart_frame_bits(
    value: int,
    *,
    data_bits: int = 8,
    parity: Parity = None,
    stop_bits: int = 1,
    msb_first: bool = False,
) -> list[int]:
    """Logical bit sequence for one UART frame (start, data, parity, stop)."""
    if not 5 <= data_bits <= 9:
        raise UsageError(f"data_bits must be 5-9, got {data_bits}")
    if value < 0 or value >= (1 << data_bits):
        raise UsageError(f"value {value} does not fit in {data_bits} bits")
    if stop_bits not in (1, 2):
        raise UsageError(f"stop_bits must be 1 or 2, got {stop_bits}")
    data = [(value >> i) & 1 for i in range(data_bits)]  # LSB-first wire order
    if msb_first:
        data.reverse()
    bits = [0, *data]
    if parity is not None:
        ones = sum(data)
        if parity == "even":
            bits.append(ones % 2)
        elif parity == "odd":
            bits.append(1 - ones % 2)
        else:
            raise UsageError(f"parity must be 'even', 'odd', or None, got {parity!r}")
    bits.extend([1] * stop_bits)
    return bits


def _expand(levels: Sequence[tuple[int, float]], samples_per_unit: float) -> list[int]:
    """Expand (level, duration_units) runs into samples with phase accumulation."""
    out: list[int] = []
    pos = 0.0
    emitted = 0
    for level, duration in levels:
        pos += duration
        target = round(pos * samples_per_unit)
        if target > emitted:
            out.extend([level] * (target - emitted))
            emitted = target
    return out


def uart_wave(
    data: bytes | Sequence[int],
    baud: int,
    *,
    sample_rate: int = SAMPLE_RATE_HZ,
    data_bits: int = 8,
    parity: Parity = None,
    stop_bits: int = 1,
    msb_first: bool = False,
    invert: bool = False,
    idle_before: int | None = None,
    idle_after: int | None = None,
    inter_byte_idle_bits: float = 0.0,
) -> list[int]:
    """Per-sample line levels for a UART byte sequence.

    Returns a list of 0/1 samples. Idle level is 1 (0 when ``invert``).
    ``idle_before``/``idle_after`` default to two bit times, enough for the
    decoder's idle-arming requirement.
    """
    spb = sample_rate / baud
    if idle_before is None:
        idle_before = round(2 * spb)
    if idle_after is None:
        idle_after = round(2 * spb)
    runs: list[tuple[int, float]] = []
    for i, value in enumerate(data):
        if i and inter_byte_idle_bits:
            runs.append((1, inter_byte_idle_bits))
        for bit in uart_frame_bits(
            value, data_bits=data_bits, parity=parity, stop_bits=stop_bits, msb_first=msb_first
        ):
            runs.append((bit, 1.0))
    body = _expand(runs, spb)
    idle = 1
    wave = [idle] * idle_before + body + [idle] * idle_after
    if invert:
        wave = [1 - s for s in wave]
    return wave


def spi_wave(
    mosi_words: Sequence[int],
    miso_words: Sequence[int] | None = None,
    *,
    clock_hz: float,
    sample_rate: int = SAMPLE_RATE_HZ,
    mode: int = 0,
    word_bits: int = 8,
    msb_first: bool = True,
    with_cs: bool = True,
    cs_active_low: bool = True,
    cs_setup_samples: int = 10,
    cs_hold_samples: int = 10,
    idle_before: int = 20,
    idle_after: int = 20,
    inter_word_idle_cycles: float = 0.0,
) -> dict[str, list[int]]:
    """Per-sample waves for an SPI transaction.

    Returns equal-length lists for keys ``sclk``, ``mosi``, ``miso``, ``cs``.
    """
    if mode not in (0, 1, 2, 3):
        raise UsageError(f"SPI mode must be 0-3, got {mode}")
    if not 4 <= word_bits <= 32:
        raise UsageError(f"word_bits must be 4-32, got {word_bits}")
    cpol, cpha = mode >> 1, mode & 1
    idle_clk = cpol
    active_clk = 1 - cpol
    samples_per_half = sample_rate / clock_hz / 2

    if miso_words is None:
        miso_words = [0] * len(mosi_words)
    if len(miso_words) != len(mosi_words):
        raise UsageError("miso_words must match mosi_words length")

    def word_to_bits(value: int) -> list[int]:
        if value < 0 or value >= (1 << word_bits):
            raise UsageError(f"word {value:#x} does not fit in {word_bits} bits")
        bits = [(value >> i) & 1 for i in range(word_bits)]
        if msb_first:
            bits.reverse()
        return bits

    # Each entry: (sclk_level, mosi_bit, miso_bit, duration_in_half_periods)
    runs: list[tuple[int, int, int, float]] = []
    last_mosi, last_miso = 0, 0
    for w, (mo, mi) in enumerate(zip(mosi_words, miso_words, strict=True)):
        if w and inter_word_idle_cycles:
            runs.append((idle_clk, last_mosi, last_miso, inter_word_idle_cycles * 2))
        for mo_bit, mi_bit in zip(word_to_bits(mo), word_to_bits(mi), strict=True):
            if cpha == 0:
                # Data valid during the idle half; sampled on the leading edge.
                runs.append((idle_clk, mo_bit, mi_bit, 1.0))
                runs.append((active_clk, mo_bit, mi_bit, 1.0))
            else:
                # Data launched on the leading edge; sampled on the trailing edge.
                runs.append((active_clk, mo_bit, mi_bit, 1.0))
                runs.append((idle_clk, mo_bit, mi_bit, 1.0))
            last_mosi, last_miso = mo_bit, mi_bit

    sclk = _expand([(r[0], r[3]) for r in runs], samples_per_half)
    mosi = _expand([(r[1], r[3]) for r in runs], samples_per_half)
    miso = _expand([(r[2], r[3]) for r in runs], samples_per_half)
    body_len = len(sclk)
    mosi += [mosi[-1] if mosi else 0] * (body_len - len(mosi))
    miso += [miso[-1] if miso else 0] * (body_len - len(miso))

    cs_active = 0 if cs_active_low else 1
    cs_idle = 1 - cs_active

    def pad(wave: list[int], level_pre: int, level_post: int) -> list[int]:
        return (
            [level_pre] * (idle_before + cs_setup_samples)
            + wave
            + [level_post] * (cs_hold_samples + idle_after)
        )

    out = {
        "sclk": pad(sclk, idle_clk, idle_clk),
        "mosi": pad(mosi, 0, 0),
        "miso": pad(miso, 0, 0),
    }
    if with_cs:
        cs_wave = (
            [cs_idle] * idle_before
            + [cs_active] * (cs_setup_samples + body_len + cs_hold_samples)
            + [cs_idle] * idle_after
        )
    else:
        total = idle_before + cs_setup_samples + body_len + cs_hold_samples + idle_after
        cs_wave = [cs_idle] * total
    out["cs"] = cs_wave
    return out


def merge_logic(
    waves: dict[int, Sequence[int]],
    length: int | None = None,
    idle_levels: dict[int, int] | None = None,
) -> bytes:
    """Merge per-channel bit waves into logic bytes (bit N = D<N>).

    Channels absent from ``waves`` stay at their ``idle_levels`` entry
    (default 0). Shorter waves are padded with the channel's idle level.
    """
    idle_levels = idle_levels or {}
    n = length if length is not None else max((len(w) for w in waves.values()), default=0)
    base = 0
    for bit, level in idle_levels.items():
        if level:
            base |= 1 << bit
    out = bytearray()
    for i in range(n):
        value = base
        for bit, wave in waves.items():
            if i < len(wave):
                level = wave[i]
            else:
                level = idle_levels.get(bit, 0)
            if level:
                value |= 1 << bit
            else:
                value &= ~(1 << bit)
        out.append(value)
    return bytes(out)
