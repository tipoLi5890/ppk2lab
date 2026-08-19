"""Schema integrity and anti-drift checks."""

import jsonschema
import pytest

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
        "window-stats",
    }
    assert expected.issubset(set(SCHEMAS))
