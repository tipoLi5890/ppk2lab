"""Event-level energy analysis and machine-readable assertions."""

from .assertions import (
    AssertionOutcome,
    AssertionRule,
    evaluate_assertion,
    junit_report,
    parse_rule,
)
from .compare import compare_stats, dominant_range, summarize_side
from .measure import (
    load_annotations_jsonl,
    measure_annotations,
    measure_window,
    save_annotations_jsonl,
)

__all__ = [
    "AssertionOutcome",
    "AssertionRule",
    "compare_stats",
    "dominant_range",
    "evaluate_assertion",
    "junit_report",
    "load_annotations_jsonl",
    "measure_annotations",
    "measure_window",
    "parse_rule",
    "save_annotations_jsonl",
    "summarize_side",
]
