"""Assertion DSL parsing and evidence-first evaluation."""

import json
import re
from xml.sax.saxutils import unescape

import pytest

from ppk2lab.analysis.assertions import (
    AssertionRule,
    evaluate_assertion,
    junit_report,
    parse_rule,
)
from ppk2lab.decoders.base import decode_capture
from ppk2lab.decoders.spi import SPIDecoder
from ppk2lab.decoders.uart import UARTDecoder, uart_runs
from ppk2lab.errors import UsageError
from ppk2lab.testing.profiles import ArrayProfile, StepProfile
from ppk2lab.testing.signals import merge_logic, spi_wave, uart_wave

from .conftest import capture_of, wave_capture

#: one whole frame time at 9600 8N1 — the idle run the UART decoder needs
#: before it will claim a frame boundary (see tests/test_uart_decoder.py)
UART_SYNC_IDLE = 104
SPI_CH = {"sclk": 1, "mosi": 2, "miso": 3, "cs": 4}
SPI_CFG = {"spi": {"sclk": "D1", "mosi": "D2", "cs": "D4"}}


def test_parse_full_rule():
    rule = parse_rule('after uart("TX_DONE"), within 20ms, avg_current < 10uA')
    assert rule.after == {"type": "uart", "match": "TX_DONE"}
    assert rule.within_s == pytest.approx(0.02)
    assert rule.metric == "mean_ua"
    assert rule.op == "<"
    assert rule.value == 10.0


def test_parse_event_variants():
    assert parse_rule('after uart("X", D2), max_current < 1mA').after["channel"] == "D2"
    assert parse_rule("after spi(0x9f, 0x00), charge < 5uC").after["pattern"] == [0x9F, 0]
    assert parse_rule("after digital(D3 rising), energy < 1mJ").after == {
        "type": "digital",
        "channel": "D3",
        "edge": "rising",
    }


def test_parse_whole_capture_rule():
    rule = parse_rule("max_current < 50mA")
    assert rule.after is None and rule.within_s is None
    assert rule.value == 50_000.0


@pytest.mark.parametrize(
    "bad",
    [
        "within 20ms, avg_current < 10uA",  # within requires after
        "avg_current < 10",  # missing unit
        "bogus_metric < 10uA",
        'after party("X"), avg_current < 10uA',
        "",
    ],
)
def test_parse_rejects_invalid(bad):
    with pytest.raises(UsageError):
        parse_rule(bad)


def test_rule_json_roundtrip():
    rule = parse_rule('after uart("GO"), within 5ms, charge < 2uC')
    again = AssertionRule.from_json(rule.to_json())
    assert again.metric == rule.metric
    assert again.value == rule.value
    assert again.after == rule.after


def make_uart_capture(sleep_ua=6.0, active_ua=8.0):
    wave = uart_wave(b"TX_DONE\n", 9600, idle_before=1000)
    n = len(wave)
    currents = [active_ua] * n + [sleep_ua] * 4000
    logic = bytes(wave) + b"\x01" * 4000
    return capture_of(
        ArrayProfile(currents, logic, default_current_ua=sleep_ua, default_logic=1),
        samples=len(logic),
    )


def test_assertion_passes_with_evidence():
    capture = make_uart_capture()
    rule = parse_rule('after uart("TX_DONE"), within 20ms, avg_current < 10uA')
    outcome = evaluate_assertion(capture, rule)
    assert outcome.status == "passed"
    assert outcome.events_found == 1
    obs = outcome.observations[0]
    assert obs["observed"] < 10.0
    assert obs["window"]["end_sample"] - obs["window"]["start_sample"] == 2000
    assert outcome.capture_sha256 == capture.sha256()


def test_assertion_fails_with_observed_value():
    capture = make_uart_capture(sleep_ua=50.0)
    rule = parse_rule('after uart("TX_DONE"), within 20ms, avg_current < 10uA')
    outcome = evaluate_assertion(capture, rule)
    assert outcome.status == "failed"
    # the window mixes the UART tail (8 uA) with 50 uA sleep; well above 10 uA
    assert outcome.observations[0]["observed"] > 40.0


def test_assertion_no_event():
    capture = make_uart_capture()
    rule = parse_rule('after uart("NEVER_SENT"), within 20ms, avg_current < 10uA')
    outcome = evaluate_assertion(capture, rule)
    assert outcome.status == "no_event"
    assert not outcome.passed


def test_gap_in_window_is_incomplete_not_pass():
    wave = uart_wave(b"TX_DONE\n", 9600, idle_before=1000)
    logic = bytes(wave) + b"\x01" * 4000
    capture = capture_of(
        ArrayProfile(6.0, logic, default_logic=1),
        samples=len(logic),
        gaps={len(wave) + 500: 100},  # gap inside the evaluation window
    )
    rule = parse_rule('after uart("TX_DONE"), within 20ms, avg_current < 10uA')
    outcome = evaluate_assertion(capture, rule)
    assert outcome.status == "incomplete"
    assert not outcome.passed
    assert outcome.observations[0]["status"] == "incomplete"


def test_whole_capture_metrics():
    capture = capture_of(StepProfile([(1000, 1000.0, 0)]), samples=1000)
    assert evaluate_assertion(capture, parse_rule("charge < 11uC")).passed
    assert not evaluate_assertion(capture, parse_rule("charge < 9uC")).passed
    assert evaluate_assertion(capture, parse_rule("energy < 31uJ")).passed
    assert evaluate_assertion(capture, parse_rule("max_current >= 900uA")).passed


def test_digital_event_windows():
    profile = StepProfile([(500, 5000.0, 0x00), (1500, 6.0, 0x08)])
    capture = capture_of(profile, samples=2000)
    rule = parse_rule("after digital(D3 rising), within 10ms, avg_current < 10uA")
    outcome = evaluate_assertion(capture, rule)
    assert outcome.status == "passed"
    assert outcome.observations[0]["window"]["start_sample"] == 500


def split_uart_capture(gap_missing=200):
    """``b"TX_"``, a sample gap, then ``b"DONE"`` — both sides decode cleanly."""
    head = uart_wave(b"TX_", 9600, idle_before=1000, idle_after=0)
    tail = uart_wave(b"DONE", 9600, idle_before=1000, idle_after=0)
    logic = bytes(head) + bytes(tail) + b"\x01" * 4000
    capture = capture_of(
        ArrayProfile(6.0, logic, default_logic=1),
        samples=len(logic),
        gaps={len(head): gap_missing},
    )
    return capture


def spi_capture(*transactions, tail_samples=3000):
    """One CS transaction per argument, back to back, no gaps.

    ``tail_samples`` of idle follow, so a ``within 20ms`` rule anchored on the
    last transaction has a capture to be evaluated over: a window that runs
    past the end of the data is reported ``incomplete``, which would mask what
    these tests are actually about.
    """
    waves: dict[str, list[int]] = {"sclk": [], "mosi": [], "miso": [], "cs": []}
    for words in transactions:
        part = spi_wave(list(words), clock_hz=10_000, mode=0)
        for name, wave in waves.items():
            wave.extend(part[name])
        for name in waves:
            waves[name].extend([1 if name == "cs" else 0] * 200)
    logic = merge_logic(
        {SPI_CH[name]: wave for name, wave in waves.items()},
        idle_levels={SPI_CH["cs"]: 1},
    )
    logic = bytes(logic) + bytes([1 << SPI_CH["cs"]]) * tail_samples
    return wave_capture(logic, current_ua=6.0)


def test_uart_anchor_must_be_contiguous_bytes():
    """``TX_DONE`` split by a gap is not evidence that ``TX_DONE`` was sent."""
    capture = split_uart_capture()
    outcome = evaluate_assertion(
        capture, parse_rule('after uart("TX_DONE"), within 20ms, avg_current < 10uA')
    )
    assert outcome.events_found == 0
    assert outcome.status == "no_event"
    # the user is told why, rather than left with a silent no_event
    assert any("never observed back to back" in w for w in outcome.warnings)

    # each half is real evidence on its own
    for text in ("TX_", "DONE"):
        found = evaluate_assertion(
            capture, parse_rule(f'after uart("{text}"), within 20ms, avg_current < 10uA')
        )
        assert found.events_found == 1, text
        assert not any("back to back" in w for w in found.warnings), text


def test_uart_anchor_found_when_contiguous():
    wave = uart_wave(b"TX_DONE", 9600, idle_before=1000, idle_after=0)
    logic = bytes(wave) + b"\x01" * 4000
    capture = capture_of(ArrayProfile(6.0, logic, default_logic=1), samples=len(logic))
    outcome = evaluate_assertion(
        capture, parse_rule('after uart("TX_DONE"), within 20ms, avg_current < 10uA')
    )
    assert outcome.events_found == 1
    assert outcome.warnings == []


def test_uart_anchor_ignores_unsynchronized_frames():
    """A capture that starts mid-frame offers no clean bytes to anchor on."""
    wave = uart_wave(b"TX_DONE", 9600, idle_before=0, idle_after=0)
    logic = bytes(wave) + b"\x01" * 4000
    capture = capture_of(ArrayProfile(6.0, logic, default_logic=1), samples=len(logic))
    annotations = decode_capture(capture, UARTDecoder(rx="D0", baud=9600))
    assert uart_runs(annotations) == []  # bit-shifted guesses, not bytes
    outcome = evaluate_assertion(
        capture, parse_rule('after uart("TX_DONE"), within 20ms, avg_current < 10uA')
    )
    assert outcome.events_found == 0


def test_spi_anchor_must_be_one_transaction():
    """Two CS transactions are two exchanges, whatever the words spell."""
    rule = parse_rule("after spi(0x9f, 0x00), within 20ms, avg_current < 10uA")

    split = evaluate_assertion(spi_capture([0x9F], [0x00]), rule, decoder_config=SPI_CFG)
    assert split.events_found == 0
    assert split.status == "no_event"
    assert any("one transaction" in w for w in split.warnings)

    together = evaluate_assertion(spi_capture([0x9F, 0x00]), rule, decoder_config=SPI_CFG)
    assert together.events_found == 1
    assert together.warnings == []


def test_spi_anchor_not_matched_across_a_gap():
    """Both words decode cleanly, but samples were lost between them."""
    waves: dict[str, list[int]] = {"sclk": [], "mosi": [], "miso": [], "cs": []}
    for words in ([0x9F], [0x00]):
        part = spi_wave(list(words), clock_hz=10_000, mode=0)
        for name, wave in waves.items():
            wave.extend(part[name])
    logic = merge_logic(
        {SPI_CH[name]: wave for name, wave in waves.items()},
        idle_levels={SPI_CH["cs"]: 1},
    )
    # the gap sits in the idle after the first transaction and ends before the
    # second asserts CS, so both words are decoded without error
    boundary = len(spi_wave([0x9F], clock_hz=10_000, mode=0)["sclk"])
    capture = wave_capture(logic, current_ua=6.0, gaps={boundary + 1: 15})
    annotations = decode_capture(capture, SPIDecoder(sclk="D1", mosi="D2", cs="D4"))
    clean = [a for a in annotations if a.kind == "word" and not a.errors]
    assert [a.fields["mosi"] for a in clean] == [0x9F, 0x00]
    outcome = evaluate_assertion(
        capture,
        parse_rule("after spi(0x9f, 0x00), within 20ms, avg_current < 10uA"),
        decoder_config=SPI_CFG,
    )
    assert outcome.events_found == 0


def test_junit_report_shape():
    capture = make_uart_capture(sleep_ua=50.0)
    outcomes = [
        evaluate_assertion(
            capture, parse_rule('after uart("TX_DONE"), within 20ms, avg_current < 10uA')
        ),
        evaluate_assertion(capture, parse_rule("max_current < 1A")),
    ]
    xml = junit_report(outcomes)
    assert xml.startswith("<?xml")
    assert 'tests="2"' in xml
    assert 'failures="1"' in xml
    assert "<failure" in xml


def test_junit_evidence_is_parseable_json():
    """The blob a CI dashboard surfaces has to be readable by a machine."""
    capture = capture_of(StepProfile([(2000, 1000.0, 0)]), samples=2000)
    xml = junit_report([evaluate_assertion(capture, parse_rule("max_current < 1A"))])
    body = re.search(r"<system-out>(.*)</system-out>", xml, re.DOTALL).group(1)
    payload = json.loads(unescape(body))
    assert payload["rule"]["metric"] == "max_current"
    assert payload["capture_sha256"] == capture.sha256()


def test_window_past_the_capture_end_is_incomplete():
    """A fifth of the asked-for window is not an answer to the question asked."""
    capture = make_uart_capture(sleep_ua=6.0)
    outcome = evaluate_assertion(
        capture, parse_rule('after uart("TX_DONE"), within 20s, avg_current < 10uA')
    )
    assert outcome.events_found == 1
    assert outcome.status == "incomplete"
    observation = outcome.observations[0]
    assert observation["reason_code"] == "window_past_capture_end"
    assert observation["capture_end_sample"] == capture.end_index
    assert observation["window"]["end_sample"] > capture.end_index
    assert any("window_past_capture_end" in w for w in outcome.warnings)


def test_window_inside_the_capture_still_passes():
    """False-alarm guard: the ordinary case must not become incomplete."""
    capture = make_uart_capture(sleep_ua=6.0)
    outcome = evaluate_assertion(
        capture, parse_rule('after uart("TX_DONE"), within 5ms, avg_current < 10uA')
    )
    assert outcome.status == "passed"
    assert outcome.observations[0]["covered_fraction"] == 1.0


@pytest.mark.parametrize(
    "bad",
    [
        {"metric": "avg_current", "op": "<", "value": float("nan")},
        {"metric": "avg_current", "op": "<", "value": float("inf")},
        {"metric": "avg_current", "op": "<", "value": "not a number"},
        {"metric": "avg_current", "op": "<"},
        {"op": "<", "value": 1.0},
        {"metric": "avg_current", "op": "~", "value": 1.0},
        {"metric": "avg_current", "op": "<", "value": 1.0, "within_s": 0},
        {"metric": "avg_current", "op": "<", "value": 1.0, "within_s": 1.0},
        {"metric": "avg_current", "op": "<", "value": 1.0, "after": "uart"},
    ],
)
def test_rule_json_refuses_uncomparable_input(bad):
    """A threshold that can never compare true or false must not reach CI.

    A NaN value silently reported ``passed: false`` with ``ok: true`` — a
    blocked merge on a comparison that never happened.
    """
    with pytest.raises(UsageError):
        AssertionRule.from_json(bad)
