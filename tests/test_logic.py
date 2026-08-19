"""Transitions, edges, pulses, and VCD export — all gap-aware."""

import io
from array import array

from ppk2lab.logic import edges, export_vcd, iter_transitions, pulses
from ppk2lab.protocol.samples import SampleBlock
from ppk2lab.types import GapEvent


def block(start, logic_bytes):
    words = array("I", [b << 24 for b in logic_bytes])
    return SampleBlock(start, words)


def test_transitions_initial_and_changes():
    events = [block(0, [0x00, 0x00, 0x01, 0x01, 0x03])]
    transitions = list(iter_transitions(events))
    assert [(t.sample_index, t.logic) for t in transitions] == [(0, 0), (2, 1), (4, 3)]
    assert transitions[0].changed_mask == 0xFF  # initial state
    assert transitions[1].changed_mask == 0x01


def test_transitions_after_gap_restate():
    events = [block(0, [0x00, 0x01]), GapEvent(2, 5), block(7, [0x01, 0x00])]
    transitions = list(iter_transitions(events))
    restated = [t for t in transitions if t.after_gap]
    assert len(restated) == 1
    assert restated[0].sample_index == 7


def test_edges_not_claimed_across_gap():
    # level differs across the gap, but that is not an observed edge
    events = [block(0, [0x00, 0x00]), GapEvent(2, 5), block(7, [0x01, 0x01])]
    assert edges(events, 0) == []
    # a real edge inside contiguous data is reported
    events = [block(0, [0x00, 0x01, 0x00])]
    result = edges(events, 0)
    assert [(e.sample_index, e.rising) for e in result] == [(1, True), (2, False)]


def test_pulses_bounded_by_observed_edges_only():
    wave = [0] * 10 + [1] * 20 + [0] * 30 + [1] * 5
    events = [block(0, wave)]
    result = pulses(events, 0)
    # first run (0..10) starts at capture edge -> excluded; last run unterminated
    assert [(p.start_sample, p.end_sample, p.level) for p in result] == [
        (10, 30, 1),
        (30, 60, 0),
    ]
    assert result[0].width_samples == 20
    assert result[0].width_s == 20 * 1e-5


def test_pulses_reset_by_gap():
    wave1 = [0] * 5 + [1] * 5
    wave2 = [1] * 5 + [0] * 5 + [1] * 3
    events = [block(0, wave1), GapEvent(10, 4), block(14, wave2)]
    result = pulses(events, 0)
    # only the high run fully inside the post-gap segment qualifies... the run
    # 14..19 started at a gap boundary, so the first valid run is 19..24
    assert [(p.start_sample, p.end_sample) for p in result] == [(19, 24)]


def test_vcd_export_marks_gaps_as_x():
    events = [block(0, [0x01, 0x01, 0x00]), GapEvent(3, 5), block(8, [0x01])]
    out = io.StringIO()
    export_vcd(events, out, channels=[0])
    text = out.getvalue()
    assert "$timescale 10 us $end" in text
    assert "$var wire 1 ! D0 $end" in text
    body = text.split("$enddefinitions $end")[1]
    assert "#0\n1!" in body
    assert "#2\n0!" in body
    assert "#3\nx!" in body  # gap drives x
    assert "#8\n1!" in body  # value re-established after the gap
