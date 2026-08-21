"""Schema integrity and anti-drift checks."""

import jsonschema
import pytest

from ppk2lab.capture.runner import timeline_report
from ppk2lab.capture.stats import StatsAccumulator
from ppk2lab.diagnostics import WARNING_CATALOG, warning_catalog
from ppk2lab.errors import ERROR_CLASSES, SchemaNotFoundError, error_catalog
from ppk2lab.schemas import SCHEMAS, get_schema, list_schemas


def test_every_schema_is_valid_jsonschema():
    validator = jsonschema.Draft202012Validator
    for name in list_schemas():
        schema = get_schema(name)
        validator.check_schema(schema)
        assert schema.get("$id") == f"ppk2lab:{name}", name


def test_unknown_schema_raises_with_remediation():
    with pytest.raises(SchemaNotFoundError):
        get_schema("bogus")


def test_error_codes_unique_and_complete():
    codes = [cls.code for cls in ERROR_CLASSES]
    assert len(codes) == len(set(codes)), "error codes must be unique"
    catalog = error_catalog()
    assert {entry["code"] for entry in catalog} == set(codes)
    for entry in catalog:
        assert entry["remediation"], f"{entry['code']} lacks remediation"
        assert 0 <= entry["exit_code"] <= 9


def test_schema_count_stability():
    # Adding a schema is fine; removing or renaming one is a breaking change
    # that requires a SCHEMA_VERSION bump. This test forces that conversation.
    expected = {
        "annotation",
        "assert-result",
        "capabilities-result",
        "capture-manifest",
        "capture-result",
        "configure-result",
        "decode-result",
        "device",
        "discover-result",
        "doctor-result",
        "envelope",
        "error",
        "export-result",
        "gap",
        "info-result",
        "measure-result",
        "state-change",
        "web-result",
        "window-stats",
    }
    assert expected.issubset(set(SCHEMAS))


def test_web_result_publishes_every_field_it_emits():
    """`ppk2lab web` returns an ordinary envelope like every other command, so
    its result is validated like every other result."""
    from ppk2lab_web.server import ServeResult

    emitted = ServeResult(
        url="http://127.0.0.1:8765/",
        host="127.0.0.1",
        port=8765,
        control_enabled=False,
        token_required=True,
        simulated=True,
    ).to_json()
    declared = get_schema("web-result")["properties"]
    assert set(emitted) == set(declared)
    jsonschema.validate(emitted, get_schema("web-result"))


def test_window_stats_publishes_every_field_it_emits():
    """A result field the schema does not declare is a field no consumer can
    validate against. Adding one to `to_json` must add it here in the same
    change, which is what this comparison forces."""
    emitted = StatsAccumulator(0).finalize().to_json()
    declared = get_schema("window-stats")["properties"]
    assert set(emitted) == set(declared)
    assert set(emitted["samples"]) == set(declared["samples"]["properties"])
    assert set(emitted["current_ua"]) == set(declared["current_ua"]["properties"])
    jsonschema.validate(emitted, get_schema("window-stats"))


def test_timeline_check_publishes_every_field_it_emits():
    report = timeline_report(
        first_sample_at=0.0,
        last_sample_at=10.0,
        timeline_advance=1_000_000,
        started_utc="",
        ended_utc="",
        first_sample_utc="",
        first_block_samples=4096,
        last_block_samples=4096,
    )
    assert set(report) == set(get_schema("timeline-check")["properties"])
    jsonschema.validate(report, get_schema("timeline-check"))


def test_every_warning_code_has_a_published_meaning():
    """The catalog is how an agent learns the vocabulary from the tool instead
    of from the source, so a code without an entry is a code nobody can act
    on."""
    from ppk2lab import diagnostics

    codes = {
        value
        for name, value in vars(diagnostics).items()
        if name.startswith("W_") and isinstance(value, str)
    }
    assert codes == set(WARNING_CATALOG)
    for entry in warning_catalog():
        assert entry["meaning"].strip(), entry["code"]


def test_every_catalog_entry_is_categorized():
    """The three catalogs share one shape, so one reader handles all three."""
    from ppk2lab.diagnostics import (
        GAP_CATEGORY,
        GAP_REASONS,
        INTERRUPTION_CATEGORY,
        INTERRUPTION_REASONS,
        WARNING_CATALOG,
        WARNING_CATEGORY,
        gap_reason_catalog,
        interruption_reason_catalog,
        warning_catalog,
    )

    # A code without a category would raise KeyError at catalog time; asserting
    # the key sets instead names the missing code.
    assert set(WARNING_CATEGORY) == set(WARNING_CATALOG)
    assert set(GAP_CATEGORY) == set(GAP_REASONS)
    assert set(INTERRUPTION_CATEGORY) == set(INTERRUPTION_REASONS)
    shapes = {
        frozenset(entry)
        for catalog in (warning_catalog(), gap_reason_catalog(), interruption_reason_catalog())
        for entry in catalog
    }
    assert shapes == {frozenset({"code", "category", "meaning"})}
