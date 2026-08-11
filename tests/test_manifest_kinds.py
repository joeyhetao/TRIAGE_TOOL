import json
from pathlib import Path

from xlog.actions import dispatch_request
from xlog.schema_validation import validate_instance


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "manifest_kinds" / "regression"
SCHEMA = json.loads((ROOT / "schemas" / "xlog_bundle.v1.schema.json").read_text(encoding="utf-8"))


def _scan(tmp_path):
    output = tmp_path / "manifest-kinds-bundle.json"
    response = dispatch_request({
        "api_version": "xlog.v1",
        "request_id": "manifest-kinds-fixture",
        "action": "scan",
        "target": {"regression_root": str(FIXTURE.resolve())},
        "args": {"output_path": str(output.resolve())},
        "limits": {"workers": 1},
    })
    assert response["ok"] is True
    return json.loads(output.read_text(encoding="utf-8"))


def _case(bundle, name):
    return next(case for case in bundle["cases"] if case["case_id"].startswith(name + "/"))


def _manifest(case, artifact_kind):
    return next(
        item for item in case["artifacts"]["manifests"]["items"]
        if item["artifact_kind"] == artifact_kind
    )


def test_manifest_kind_fixture_exposes_preference_and_controlled_fallbacks(tmp_path):
    bundle = _scan(tmp_path)

    assert bundle["schema_revision"] == "1.3"
    assert validate_instance(bundle, SCHEMA) is True

    both = _case(bundle, "both_1")
    assert both["artifacts"]["manifests"]["selection_status"] == "preferred"
    assert both["artifacts"]["manifests"]["selected"]["artifact_kind"] == "xdebug.run_manifest"
    assert _manifest(both, "xdebug.run_manifest")["parse_status"] == "parsed"
    assert _manifest(both, "xvp.case_manifest")["parse_status"] == "parsed"
    assert both["artifacts"]["xdebug_target"]["run_manifest"].endswith("xdebug.run-manifest.v1.json")

    legacy = _case(bundle, "legacy_only_1")
    assert legacy["artifacts"]["manifests"]["selection_status"] == "legacy_fallback"
    assert legacy["artifacts"]["manifests"]["selected"]["artifact_kind"] == "xvp.case_manifest"
    assert _manifest(legacy, "xdebug.run_manifest")["resolution_status"] == "unavailable"
    assert "run_manifest" not in legacy["artifacts"]["xdebug_target"]

    missing = _case(bundle, "xdebug_missing_1")
    missing_xdebug = _manifest(missing, "xdebug.run_manifest")
    assert missing["artifacts"]["manifests"]["selection_status"] == "legacy_fallback"
    assert missing_xdebug["resolution_status"] == "unavailable"
    assert missing_xdebug["parse_status"] == "not_parsed"
    assert missing_xdebug["reason"] == "no_existing_candidates"
    assert missing_xdebug["path"].endswith("xdebug.run-manifest.v1.json")

    mismatch = _case(bundle, "schema_mismatch_1")
    mismatch_xdebug = _manifest(mismatch, "xdebug.run_manifest")
    assert mismatch["artifacts"]["manifests"]["selection_status"] == "legacy_fallback"
    assert mismatch_xdebug["resolution_status"] == "resolved"
    assert mismatch_xdebug["schema_version"] == "xdebug.run-manifest.v2"
    assert mismatch_xdebug["parse_status"] == "schema_mismatch"
    assert "run_manifest" not in mismatch["artifacts"]["xdebug_target"]

    ambiguous = _case(bundle, "ambiguous_1")
    ambiguous_xdebug = _manifest(ambiguous, "xdebug.run_manifest")
    assert ambiguous["artifacts"]["manifests"]["selection_status"] == "legacy_fallback"
    assert ambiguous_xdebug["resolution_status"] == "ambiguous"
    assert ambiguous_xdebug["parse_status"] == "not_parsed"
    assert ambiguous_xdebug["path"] is None
    assert len([item for item in ambiguous_xdebug["candidates"] if item["state"] == "available"]) == 2
