import json

import pytest

from xlog.actions import dispatch_request
from xlog.errors import XlogError


def _request(root, output, **extra):
    request = {
        "api_version": "xlog.v1",
        "request_id": "scan-test",
        "action": "scan",
        "target": {"regression_root": str(root)},
        "args": {"output_path": str(output)},
    }
    request.update(extra)
    return request


def test_scan_writes_bundle_with_sorted_cases_clusters_and_recommendation(tmp_path):
    root = tmp_path / "regression"
    (root / "z").mkdir(parents=True)
    (root / "a").mkdir()
    (root / "z" / "case_9.log").write_text("UVM_ERROR /tb/rpe.sv(9) @ 10ns: reporter [QP] ceq of function=7fc is full\n", encoding="utf-8")
    (root / "a" / "case_2.log").write_text("UVM_ERROR /tb/rpe.sv(9) @ 100ns: reporter [QP] ceq of function=187 is full\n", encoding="utf-8")
    (root / "z" / "case_9.fsdb").write_bytes(b"fsdb")
    (root / "z" / "simv.daidir").mkdir()
    (root / "z" / "simv.daidir" / "kdb").mkdir()
    (root / "a" / "pass.LOG").write_text("JVP TEST PASSED\n", encoding="utf-8")
    output = tmp_path / "run" / "xlog_bundle.json"

    response = dispatch_request(_request(root, output, limits={"workers": 1}))
    bundle = json.loads(output.read_text(encoding="utf-8"))
    assert response["ok"] is True
    assert bundle["schema_revision"] == "1.4"
    assert bundle["cases"][0]["primary_error"]["description_template_status"] == "present"
    assert bundle["cases"][0]["primary_error"]["description_template"] == "ceq of function=<num> is full"
    assert response["summary"]["cases_total"] == 3
    assert [case["case_id"] for case in bundle["cases"]] == ["a/case_2.log", "a/pass.LOG", "z/case_9.log"]
    assert bundle["cases"][0]["test_id"] == "case"
    assert bundle["cases"][0]["seed"] == 2
    assert bundle["cases"][0]["seed_parse_status"] == "parsed"
    assert bundle["cases"][0]["simulation_time"]["normalized_fs"] == "100000000"
    assert bundle["summary"]["failure_clusters"] == 1
    assert bundle["summary"]["debug_recommended_cases"] == 1
    assert bundle["failure_clusters"][0]["case_ids"] == ["a/case_2.log", "z/case_9.log"]
    assert bundle["failure_clusters"][0]["representative_case_id"] == "a/case_2.log"
    assert bundle["failure_clusters"][0]["recommendation"]["recommended_case_id"] == "z/case_9.log"
    assert bundle["failure_clusters"][0]["recommendation"]["recommended_case"]["seed"] == 9
    assert bundle["failure_clusters"][0]["recommendation"]["alternate_cases"][0]["case_id"] == "a/case_2.log"
    assert bundle["failure_clusters"][0]["recommendation"]["recommended_simulation_time"]["value"] == "10"
    assert bundle["failure_clusters"][0]["recommendation"]["recommended_artifacts"]["xdebug_target"] == {
        "fsdb": str(root / "z" / "case_9.fsdb"),
        "daidir": str(root / "z" / "simv.daidir"),
    }
    assert bundle["debug_recommendation"]["policy_version"] == "deterministic.v2"
    assert bundle["debug_recommendation"]["debug_budget"] == 20
    assert bundle["debug_recommendation"]["recommended_debug_cases"][0]["case_id"] == "z/case_9.log"
    assert bundle["debug_recommendation"]["recommended_debug_cases"][0]["artifacts"]["status"] == "complete"
    assert bundle["debug_recommendation"]["recommended_debug_cases"][0]["alternate_cases"][0]["artifacts"]["status"] == "unavailable"
    assert bundle["summary"]["result_state_counts"] == {
        "PASS": 1,
        "FAIL_WITH_ERROR": 2,
        "INCOMPLETE_NO_ERROR": 0,
        "PARSE_ERROR": 0,
    }
    assert bundle["diagnostic_candidates"] == []


def test_scan_exposes_error_and_incomplete_cases_without_fake_primary_errors(tmp_path):
    root = tmp_path / "regression"
    root.mkdir()
    for index in range(3):
        (root / ("uvm_error_%d.log" % index)).write_text(
            "UVM_ERROR /tb/dut.sv(%d) @ 1ns: reporter [ERR%d] mismatch\n" % (index + 1, index),
            encoding="utf-8",
        )
    incomplete = (
        ("watchdog.log", "sim_end_watch_dog fired\n"),
        ("inactive.log", "bus inactivity detected\n"),
        ("qp.log", "QP not complete\n"),
    )
    for name, content in incomplete:
        (root / name).write_text(content, encoding="utf-8")
    output = tmp_path / "bundle.json"

    response = dispatch_request(_request(root, output, limits={"workers": 2}))
    bundle = json.loads(output.read_text(encoding="utf-8"))

    assert response["ok"] is True
    assert bundle["summary"]["result_state_counts"] == {
        "PASS": 0,
        "FAIL_WITH_ERROR": 3,
        "INCOMPLETE_NO_ERROR": 3,
        "PARSE_ERROR": 0,
    }
    assert len(bundle["failure_clusters"]) == 3
    assert [item["case_id"] for item in bundle["diagnostic_candidates"]] == [
        "inactive.log",
        "qp.log",
        "watchdog.log",
    ]
    for case in bundle["cases"]:
        if case["result_state"] == "INCOMPLETE_NO_ERROR":
            assert case["primary_error"] is None
            assert case["diagnostic_hints"]


def test_scan_debug_budget_limits_recommendations(tmp_path):
    root = tmp_path / "regression"
    root.mkdir()
    for index in range(3):
        (root / ("test_%d_1.log" % index)).write_text(
            "UVM_ERROR /tb/rpe.sv(%d) @ 1ns: reporter [E%d] failure\n" % (index, index),
            encoding="utf-8",
        )
    output = tmp_path / "bundle.json"

    response = dispatch_request(_request(root, output, limits={"workers": 1, "debug_budget": 2}))
    bundle = json.loads(output.read_text(encoding="utf-8"))

    assert response["ok"] is True
    assert bundle["summary"]["failure_clusters"] == 3
    assert bundle["debug_recommendation"]["debug_budget"] == 2
    assert bundle["debug_recommendation"]["selected_cluster_count"] == 2
    assert len(bundle["debug_recommendation"]["recommended_debug_cases"]) == 2
    assert len(bundle["debug_recommendation"]["deferred_cluster_ids"]) == 1


def test_scan_rejects_invalid_debug_budget(tmp_path):
    root = tmp_path / "regression"
    root.mkdir()
    (root / "case.log").write_text("JVP TEST PASSED\n", encoding="utf-8")
    with pytest.raises(XlogError) as error:
        dispatch_request(_request(root, tmp_path / "bundle.json", limits={"debug_budget": 0}))
    assert error.value.code == "INVALID_REQUEST"


def test_scan_records_effective_config_and_inline_override(tmp_path):
    root = tmp_path / "regression"
    root.mkdir()
    (root / "case.log").write_text("PROJECT_FATAL [P1] bad\nDONE\n", encoding="utf-8")
    config_path = tmp_path / "parser.json"
    config_path.write_text(json.dumps({"extra_patterns": ["PROJECT_FATAL"], "pass_patterns": ["DONE"]}), encoding="utf-8")
    output = tmp_path / "bundle.json"
    response = dispatch_request(_request(root, output, args={"output_path": str(output), "config_path": str(config_path), "parser": {"pass_patterns": []}}))
    bundle = json.loads(output.read_text(encoding="utf-8"))
    assert response["ok"] is True
    assert bundle["parser_config"]["extra_patterns"] == ["PROJECT_FATAL"]
    assert bundle["parser_config"]["pass_patterns"] == []
    assert bundle["cases"][0]["primary_error"]["error_id"] == "P1"


def test_scan_excludes_rerun_backup_logs(tmp_path):
    root = tmp_path / "regression"
    root.mkdir()
    (root / "tc_map_active_and_rst_mqp_with_running_967836231.log").write_text(
        "UVM_ERROR /tb/dut.sv(1) @ 1ns: reporter [PRIMARY] primary failure\n",
        encoding="utf-8",
    )
    (root / "tc_map_active_and_rst_mqp_with_running_967836231_bk.LOG").write_text(
        "UVM_ERROR /tb/dut.sv(2) @ 2ns: reporter [BACKUP] rerun backup failure\n",
        encoding="utf-8",
    )
    output = tmp_path / "bundle.json"

    response = dispatch_request(_request(root, output, limits={"workers": 1}))
    bundle = json.loads(output.read_text(encoding="utf-8"))

    assert response["ok"] is True
    assert bundle["summary"]["cases_total"] == 1
    assert [case["case_id"] for case in bundle["cases"]] == [
        "tc_map_active_and_rst_mqp_with_running_967836231.log"
    ]
    assert bundle["cases"][0]["primary_error"]["error_id"] == "PRIMARY"


def test_scan_rejects_empty_root_and_unknown_fields(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    with pytest.raises(XlogError) as error:
        dispatch_request(_request(root, tmp_path / "bundle.json"))
    assert error.value.code == "NO_LOG_FILES"
    with pytest.raises(XlogError) as error:
        dispatch_request({"api_version": "xlog.v1", "action": "actions", "unexpected": True})
    assert error.value.code == "INVALID_REQUEST"
