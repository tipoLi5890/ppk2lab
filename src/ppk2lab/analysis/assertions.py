"""Reproducible power/protocol assertions for regression testing and CI.

Rules exist in two equivalent forms:

- a human DSL: ``after uart("TX_DONE"), within 20ms, avg_current < 10uA``
- a JSON form used by agents and stored with results.

Evaluation is evidence-first: every observation records the exact sample
window; a window touching missing data yields status ``incomplete`` (CLI
exit 6), never a false pass or fail. Results embed the capture id and
SHA-256 so a conclusion can always be traced to its raw capture.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..capture.model import Capture
from ..capture.stats import compute_stats
from ..decoders.base import decode_capture
from ..decoders.spi import SPIDecoder
from ..decoders.uart import UARTDecoder, uart_bytes
from ..errors import UsageError
from ..logic.transitions import edges as logic_edges
from ..types import SAMPLE_RATE_HZ
from ..units import (
    parse_channel,
    parse_charge_uc,
    parse_current_ua,
    parse_duration_s,
    parse_energy_uj,
)

_METRICS = {
    "avg_current": ("mean_ua", "current"),
    "mean_current": ("mean_ua", "current"),
    "max_current": ("max_ua", "current"),
    "peak_current": ("max_ua", "current"),
    "min_current": ("min_ua", "current"),
    "charge": ("charge_uc", "charge"),
    "energy": ("energy_uj", "energy"),
}

_METRIC_UNITS = {"current": "uA", "charge": "uC", "energy": "uJ"}

_RULE_RE = re.compile(
    r"^\s*(?:after\s+(?P<after>.+?)\s*,)?"
    r"(?:\s*within\s+(?P<within>[0-9.]+\s*(?:s|ms|us|min)?)\s*,)?"
    r"\s*(?P<metric>[a-z_]+)\s*(?P<op>[<>]=?)\s*(?P<value>\S+)\s*$",
    re.IGNORECASE,
)
_UART_EVENT_RE = re.compile(
    r'^uart\(\s*"(?P<text>[^"]+)"\s*(?:,\s*(?P<ch>D[0-7])\s*)?\)$', re.IGNORECASE
)
_SPI_EVENT_RE = re.compile(
    r"^spi\(\s*(?P<words>(?:0x[0-9a-fA-F]+|\d+)(?:\s*,\s*(?:0x[0-9a-fA-F]+|\d+))*)\s*\)$"
)
_DIGITAL_EVENT_RE = re.compile(
    r"^digital\(\s*(?P<ch>D[0-7])\s+(?P<edge>rising|falling)\s*\)$", re.IGNORECASE
)


@dataclass
class AssertionRule:
    metric: str  # canonical stats field: mean_ua / max_ua / min_ua / charge_uc / energy_uj
    metric_name: str  # as written, e.g. avg_current
    op: str
    value: float  # canonical units: uA / uC / uJ
    value_text: str
    after: dict[str, Any] | None = None
    within_s: float | None = None
    source: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "metric": self.metric_name,
            "op": self.op,
            "value": self.value,
            "unit": _METRIC_UNITS[_METRICS[self.metric_name][1]],
            "value_text": self.value_text,
            "after": self.after,
            "within_s": self.within_s,
            "source": self.source,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> AssertionRule:
        metric_name = data["metric"]
        if metric_name not in _METRICS:
            raise UsageError(
                f"unknown metric {metric_name!r}; use one of {', '.join(sorted(_METRICS))}"
            )
        field_name, _kind = _METRICS[metric_name]
        op = data["op"]
        if op not in ("<", "<=", ">", ">="):
            raise UsageError(f"unknown comparison operator {op!r}")
        return cls(
            metric=field_name,
            metric_name=metric_name,
            op=op,
            value=float(data["value"]),
            value_text=data.get("value_text", str(data["value"])),
            after=data.get("after"),
            within_s=data.get("within_s"),
            source=data.get("source", "json"),
        )


def _parse_event(text: str) -> dict[str, Any]:
    text = text.strip()
    match = _UART_EVENT_RE.match(text)
    if match:
        event: dict[str, Any] = {"type": "uart", "match": match.group("text")}
        if match.group("ch"):
            event["channel"] = match.group("ch").upper()
        return event
    match = _SPI_EVENT_RE.match(text)
    if match:
        return {
            "type": "spi",
            "pattern": [int(w.strip(), 0) for w in match.group("words").split(",")],
        }
    match = _DIGITAL_EVENT_RE.match(text)
    if match:
        return {
            "type": "digital",
            "channel": match.group("ch").upper(),
            "edge": match.group("edge").lower(),
        }
    raise UsageError(
        f"unrecognized event in rule: {text!r}",
        remediation='Events: uart("TEXT"), uart("TEXT", D2), spi(0x9f, 0x00), digital(D3 rising).',
    )


def parse_rule(text: str) -> AssertionRule:
    """Parse the assertion DSL into a rule."""
    match = _RULE_RE.match(text)
    if not match:
        raise UsageError(
            f"cannot parse rule: {text!r}",
            remediation="Rule form: [after <event>,] [within <duration>,] "
            '<metric> <op> <value>, e.g. \'after uart("TX_DONE"), within 20ms, '
            "avg_current < 10uA'.",
        )
    metric_name = match.group("metric").lower()
    if metric_name not in _METRICS:
        raise UsageError(
            f"unknown metric {metric_name!r}; use one of {', '.join(sorted(_METRICS))}"
        )
    field_name, kind = _METRICS[metric_name]
    value_text = match.group("value")
    if kind == "current":
        value = parse_current_ua(value_text)
    elif kind == "charge":
        value = parse_charge_uc(value_text)
    else:
        value = parse_energy_uj(value_text)
    after = _parse_event(match.group("after")) if match.group("after") else None
    within_s = parse_duration_s(match.group("within")) if match.group("within") else None
    if within_s is not None and after is None:
        raise UsageError("'within' requires an 'after <event>' clause")
    return AssertionRule(
        metric=field_name,
        metric_name=metric_name,
        op=match.group("op"),
        value=value,
        value_text=value_text,
        after=after,
        within_s=within_s,
        source=text,
    )


@dataclass
class AssertionOutcome:
    rule: AssertionRule
    status: str  # passed | failed | no_event | incomplete
    observations: list[dict[str, Any]] = field(default_factory=list)
    events_found: int = 0
    capture_id: str = ""
    capture_sha256: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.status == "passed"

    def to_json(self) -> dict[str, Any]:
        return {
            "rule": self.rule.to_json(),
            "status": self.status,
            "passed": self.passed,
            "events_found": self.events_found,
            "observations": list(self.observations),
            "capture_id": self.capture_id,
            "capture_sha256": self.capture_sha256,
            "warnings": list(self.warnings),
        }


def _compare(observed: float, op: str, value: float) -> bool:
    if op == "<":
        return observed < value
    if op == "<=":
        return observed <= value
    if op == ">":
        return observed > value
    return observed >= value


def _find_event_windows(
    capture: Capture,
    rule: AssertionRule,
    decoder_config: dict[str, Any],
    *,
    allow_experimental: bool,
) -> list[tuple[int, int]]:
    assert rule.after is not None
    within = round(rule.within_s * SAMPLE_RATE_HZ) if rule.within_s is not None else None
    windows: list[tuple[int, int]] = []
    event = rule.after
    if event["type"] == "uart":
        uart_cfg = dict(decoder_config.get("uart", {}))
        channel = event.get("channel") or uart_cfg.pop("rx", "D0")
        decoder = UARTDecoder(
            rx=channel,
            baud=uart_cfg.pop("baud", 9600),
            allow_experimental=allow_experimental,
            **uart_cfg,
        )
        annotations = decode_capture(capture, decoder)
        data, ends = uart_bytes(annotations)
        pattern = event["match"].encode("ascii")
        start = 0
        while True:
            hit = data.find(pattern, start)
            if hit < 0:
                break
            windows.append((ends[hit + len(pattern) - 1], 0))
            start = hit + 1
    elif event["type"] == "spi":
        spi_cfg = dict(decoder_config.get("spi", {}))
        if "sclk" not in spi_cfg:
            raise UsageError("spi(...) events need SPI channel configuration (--spi-sclk etc.)")
        spi_decoder = SPIDecoder(allow_experimental=allow_experimental, **spi_cfg)
        annotations = decode_capture(capture, spi_decoder)
        words = [
            (a.fields.get("mosi"), a.end_sample)
            for a in annotations
            if a.kind == "word" and not a.errors
        ]
        pattern_words = event["pattern"]
        for i in range(len(words) - len(pattern_words) + 1):
            if [w for w, _ in words[i : i + len(pattern_words)]] == pattern_words:
                windows.append((words[i + len(pattern_words) - 1][1], 0))
    elif event["type"] == "digital":
        channel = parse_channel(event["channel"])
        rising = event["edge"] == "rising"
        for edge in logic_edges(capture.iter_events(), channel):
            if edge.rising == rising:
                windows.append((edge.sample_index, 0))
    else:
        raise UsageError(f"unknown event type {event['type']!r}")
    end_default = capture.end_index
    return [
        (start, min(end_default, start + within) if within is not None else end_default)
        for start, _ in windows
    ]


def evaluate_assertion(
    capture: Capture,
    rule: AssertionRule,
    *,
    decoder_config: dict[str, Any] | None = None,
    allow_experimental: bool = False,
) -> AssertionOutcome:
    outcome = AssertionOutcome(
        rule=rule,
        status="passed",
        capture_id=capture.meta.capture_id,
        capture_sha256=capture.sha256(),
    )
    if rule.after is not None:
        windows = _find_event_windows(
            capture, rule, decoder_config or {}, allow_experimental=allow_experimental
        )
        outcome.events_found = len(windows)
        if not windows:
            outcome.status = "no_event"
            outcome.warnings.append(
                "the 'after' event was not found in the capture; the assertion cannot be verified"
            )
            return outcome
    else:
        windows = [(capture.start_index, capture.end_index)]
        outcome.events_found = 1

    any_incomplete = False
    all_passed = True
    for start, end in windows:
        stats = compute_stats(capture, start_index=start, end_index=end)
        observed = getattr(stats, rule.metric)
        observation: dict[str, Any] = {
            "window": {"start_sample": start, "end_sample": end},
            "window_s": {
                "start": capture.index_to_time(start),
                "end": capture.index_to_time(end),
            },
            "metric": rule.metric_name,
            "observed": observed,
            "threshold": rule.value,
            "op": rule.op,
            "gaps_in_window": stats.gap_count,
        }
        if stats.gap_count > 0 or stats.has_unknown_gaps:
            observation["status"] = "incomplete"
            observation["reason"] = "sample gaps overlap the evaluation window"
            any_incomplete = True
        elif observed is None:
            observation["status"] = "incomplete"
            observation["reason"] = (
                "metric not computable (missing calibration or unknown source voltage)"
            )
            any_incomplete = True
        else:
            passed = _compare(observed, rule.op, rule.value)
            observation["status"] = "passed" if passed else "failed"
            all_passed = all_passed and passed
        outcome.observations.append(observation)

    if any_incomplete:
        outcome.status = "incomplete"
        outcome.warnings.append(
            "one or more evaluation windows overlap missing data; result is neither pass nor fail"
        )
    elif not all_passed:
        outcome.status = "failed"
    return outcome


def junit_report(outcomes: list[AssertionOutcome], *, suite_name: str = "ppk2lab") -> str:
    """Render outcomes as a JUnit XML report for CI systems."""
    from xml.sax.saxutils import escape, quoteattr

    failures = sum(1 for o in outcomes if o.status == "failed")
    errors = sum(1 for o in outcomes if o.status in ("no_event", "incomplete"))
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<testsuite name={quoteattr(suite_name)} tests="{len(outcomes)}" '
        f'failures="{failures}" errors="{errors}">',
    ]
    for outcome in outcomes:
        name = outcome.rule.source or outcome.rule.metric_name
        lines.append(f"  <testcase name={quoteattr(name)}>")
        if outcome.status == "failed":
            detail = "; ".join(
                f"observed {o['observed']} vs {o['op']} {o['threshold']}"
                for o in outcome.observations
                if o.get("status") == "failed"
            )
            lines.append(f"    <failure message={quoteattr(detail)} />")
        elif outcome.status in ("no_event", "incomplete"):
            message = "; ".join(outcome.warnings) or outcome.status
            lines.append(f"    <error message={quoteattr(message)} />")
        lines.append(f"    <system-out>{escape(str(outcome.to_json()))}</system-out>")
        lines.append("  </testcase>")
    lines.append("</testsuite>")
    return "\n".join(lines) + "\n"
