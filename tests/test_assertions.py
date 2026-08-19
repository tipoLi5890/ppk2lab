"""Assertion DSL parsing and evidence-first evaluation."""

import pytest

from ppk2lab.analysis.assertions import (
    AssertionRule,
    evaluate_assertion,
    junit_report,
    parse_rule,
)
from ppk2lab.errors import UsageError
from ppk2lab.testing.profiles import ArrayProfile, StepProfile
from ppk2lab.testing.signals import uart_wave

from .conftest import capture_of


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
