"""Framing, counter wrap, gap detection, and host-drop realignment."""

import struct

import pytest

from ppk2lab.protocol.samples import (
    MAX_VALID_RANGE,
    SampleBlock,
    SampleStreamParser,
    pack_sample,
    unpack_sample,
)
from ppk2lab.types import GapEvent


def word_bytes(adc=0, range_index=0, counter=0, logic=0):
    return struct.pack("<I", pack_sample(adc, range_index, counter, logic))


def stream(n, start_counter=0, logic=0):
    out = bytearray()
    for i in range(n):
        out += word_bytes(adc=i & 0x3FFF, counter=(start_counter + i) & 0x3F, logic=logic)
    return bytes(out)


def collect(events):
    blocks = [e for e in events if isinstance(e, SampleBlock)]
    gaps = [e for e in events if isinstance(e, GapEvent)]
    return blocks, gaps


def test_pack_unpack_roundtrip():
    sample = unpack_sample(pack_sample(0x1234, 3, 42, 0xA5))
    assert sample.adc == 0x1234
    assert sample.range_index == 3
    assert sample.counter == 42
    assert sample.logic == 0xA5
    assert sample.valid_range


def test_bitfield_layout():
    # bits 0-13 ADC, 14-16 range, 18-23 counter, 24-31 logic
    word = pack_sample(0x3FFF, 0x7, 0x3F, 0xFF)
    assert word == 0x3FFF | (0x7 << 14) | (0x3F << 18) | (0xFF << 24)


def test_invalid_range_flagged_not_dropped():
    parser = SampleStreamParser()
    data = word_bytes(range_index=6, counter=0) + word_bytes(range_index=2, counter=1)
    blocks, gaps = collect(parser.feed(data))
    assert not gaps
    assert len(blocks) == 1
    assert blocks[0].valid == [False, True]
    assert blocks[0].ranges[0] == 6  # raw value preserved
    assert MAX_VALID_RANGE < 6


@pytest.mark.parametrize("chunking", [1, 2, 3, 4, 5, 7, 16, 1000])
def test_arbitrary_chunk_boundaries(chunking):
    data = stream(100)
    parser = SampleStreamParser()
    total = 0
    for i in range(0, len(data), chunking):
        blocks, gaps = collect(parser.feed(data[i : i + chunking]))
        assert not gaps
        total += sum(len(b) for b in blocks)
    assert total == 100
    assert parser.next_index == 100


def test_counter_wrap_is_not_a_gap():
    data = stream(200)  # wraps 63 -> 0 three times
    parser = SampleStreamParser()
    blocks, gaps = collect(parser.feed(data))
    assert not gaps
    assert sum(len(b) for b in blocks) == 200


def test_counter_gap_detected_with_modulo_distance():
    data = stream(10)
    # skip 5 samples: counter jumps from 9 to 15
    data += word_bytes(counter=15)
    parser = SampleStreamParser()
    blocks, gaps = collect(parser.feed(data))
    assert len(gaps) == 1
    assert gaps[0].index == 10
    assert gaps[0].missing == 5
    assert gaps[0].ambiguous  # could be 5 + 64k
    # timeline advances across the gap: last sample sits at index 15
    assert blocks[-1].start_index == 15
    assert parser.next_index == 16


def test_gap_across_counter_wrap():
    data = word_bytes(counter=62)
    data += word_bytes(counter=2)  # 63, 0, 1 missing -> 3 samples
    parser = SampleStreamParser()
    _, gaps = collect(parser.feed(data))
    assert len(gaps) == 1
    assert gaps[0].missing == 3


@pytest.mark.parametrize("buffered,dropped", [(0, 8), (1, 2), (3, 6), (2, 7), (0, 5)])
def test_host_drop_realignment(buffered, dropped):
    """After dropping N bytes the parser realigns and counts lost samples."""
    parser = SampleStreamParser()
    parser.feed(stream(10))
    lost_run = buffered + dropped
    lost_samples = lost_run // 4 + (1 if lost_run % 4 else 0)

    full = stream(64, start_counter=10)  # continuation of the stream
    parser.feed(full[:buffered])
    gap = parser.notify_dropped_bytes(dropped)
    assert gap.missing == lost_samples
    assert gap.reason == "host_overflow"

    blocks, gaps = collect(parser.feed(full[buffered + dropped :]))
    assert not gaps, "post-drop counter check must not report extra loss"
    total = sum(len(b) for b in blocks)
    assert total == 64 - lost_samples
    assert blocks[0].start_index == 10 + lost_samples
    assert parser.next_index == 10 + 64


def test_device_gap_inside_host_drop_still_detected():
    parser = SampleStreamParser()
    parser.feed(stream(10))
    parser.notify_dropped_bytes(8)  # 2 samples lost on the host
    # device also skipped 4 samples inside the dropped region:
    # next counter is 10 + 2 + 4 = 16
    _blocks, gaps = collect(parser.feed(word_bytes(counter=16)))
    assert len(gaps) == 1
    assert gaps[0].missing == 4


def test_unknown_gap_degrades_timeline_and_resyncs():
    parser = SampleStreamParser()
    parser.feed(stream(10))
    gap = parser.notify_unknown_gap()
    assert gap.missing is None
    assert parser.timeline_degraded
    # counter expectation resyncs on the next sample regardless of value
    blocks, gaps = collect(parser.feed(word_bytes(counter=33)))
    assert not gaps
    assert len(blocks) == 1


def test_stats_summary():
    parser = SampleStreamParser()
    parser.feed(stream(10) + word_bytes(counter=15))
    stats = parser.flush_stats()
    assert stats["samples"] == 11
    assert stats["gaps"] == 1
    assert stats["missing_samples_known"] == 5
    assert stats["has_unknown_gaps"] is False
