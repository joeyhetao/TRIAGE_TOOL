import json
from pathlib import Path

from xlog.actions import dispatch_request


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "rtl_injection_minimal"


def _scan_fixture(tmp_path):
    output = tmp_path / "bundle.json"
    response = dispatch_request({
        "api_version": "xlog.v1",
        "request_id": "rtl-injection-fixture",
        "action": "scan",
        "target": {"regression_root": str((FIXTURE / "regression").resolve())},
        "args": {"output_path": str(output.resolve())},
        "limits": {"workers": 1, "debug_budget": 20},
    })
    assert response["ok"] is True
    return json.loads(output.read_text(encoding="utf-8"))


def test_rtl_injection_fixture_exposes_stable_facts_and_alternates(tmp_path):
    bundle = _scan_fixture(tmp_path)
    assert bundle["schema_revision"] == "1.3"

    public_cluster = next(
        cluster for cluster in bundle["failure_clusters"]
        if cluster["signature"]["scope_hint"]["candidate"] == "shared_public"
    )
    assert public_cluster["case_ids"] == ["public_compile_11.log", "public_compile_12.log"]
    assert public_cluster["signature"]["strategy"] == "level_error_id_producer_portable_description"
    assert public_cluster["signature"]["description_template"] == "<path> injected width mismatch value <num>"

    private_cluster = next(
        cluster for cluster in bundle["failure_clusters"]
        if cluster["signature"]["error_id"] == "QUEUE_FULL"
    )
    recommendation = private_cluster["recommendation"]
    assert recommendation["recommended_case"]["case_id"] == "private_missing/private_queue_21.log"
    assert recommendation["recommended_case"]["artifacts"]["status"] == "unavailable"
    assert recommendation["alternate_cases"][0]["case_id"] == "private_complete/private_queue_22.log"
    assert recommendation["alternate_cases"][0]["artifacts"]["status"] == "complete"

    ambiguous_case = next(case for case in bundle["cases"] if case["case_id"] == "ambiguous_wave_31.log")
    assert ambiguous_case["artifacts"]["status"] == "ambiguous"
    assert ambiguous_case["artifacts"]["resources"]["fsdb"]["status"] == "ambiguous"
    assert ambiguous_case["artifacts"]["resources"]["fsdb"]["reason"] == "multiple_best_candidates"


def test_canonical_bundle_fixture_is_importable_and_contains_no_root_cause():
    bundle = json.loads((FIXTURE / "xlog_bundle.fixture.json").read_text(encoding="utf-8"))
    assert bundle["api_version"] == "xlog_bundle.v1"
    assert bundle["schema_revision"] == "1.3"
    assert bundle["source"]["regression_root"] == "/fixture/rtl_injection_minimal"

    def keys(value):
        if isinstance(value, dict):
            for key, item in value.items():
                yield key
                yield from keys(item)
        elif isinstance(value, list):
            for item in value:
                yield from keys(item)

    assert "root_cause" not in set(keys(bundle))
