import json
from pathlib import Path

import pytest

from xlog.schema_validation import SchemaValidationError, validate_instance


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "schemas" / "xlog_bundle.v1.schema.json").read_text(encoding="utf-8"))


def test_canonical_fixture_validates_against_published_schema():
    fixture = json.loads(
        (ROOT / "fixtures" / "rtl_injection_minimal" / "xlog_bundle.fixture.json").read_text(encoding="utf-8")
    )

    assert validate_instance(fixture, SCHEMA) is True
    assert fixture["api_version"] == "xlog_bundle.v1"
    assert fixture["schema_revision"] == "1.2"


def test_schema_validation_reports_contract_violations():
    with pytest.raises(SchemaValidationError) as error:
        validate_instance({"api_version": "wrong"}, SCHEMA)

    assert any("expected constant" in item for item in error.value.errors)
    assert any("missing required property generated_at" in item for item in error.value.errors)
