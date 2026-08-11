import json
from pathlib import Path

from xlog.actions import dispatch_request
from xlog.schema_validation import validate_instance


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "schemas" / "xlog_bundle.v1.schema.json").read_text(encoding="utf-8"))
CASE_NAMES = (
    "baseline_clean_1",
    "dut_rdata_flip_1",
    "dut_mem_addr_shift_1",
    "env_bridge_rsp_flip_1",
    "env_cpu_rsp_vif_swap_1",
)


def _artifact_config(project_root):
    config_path = project_root / "cfg" / "xlog_scan.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps({
            "artifacts": {
                "log_reference_extraction": True,
                "fsdb_templates": ["{log_dir}/{log_stem}.fsdb"],
                "daidir_templates": ["{log_dir}/../../build/simv.daidir"],
                "kdb_templates": ["{log_dir}/../../build/simv.daidir/kdb.elab++"],
                "run_manifest_templates": ["{log_dir}/xvp_case_manifest.json"],
            }
        }),
        encoding="utf-8",
    )
    return config_path


def _scan(scan_root, config_path, output):
    response = dispatch_request({
        "api_version": "xlog.v1",
        "request_id": "cache-layout-test",
        "action": "scan",
        "target": {"regression_root": str(scan_root)},
        "args": {"output_path": str(output), "config_path": str(config_path)},
        "limits": {"workers": 1, "debug_budget": 20},
    })
    assert response["ok"] is True
    return json.loads(output.read_text(encoding="utf-8"))


def _summary_lines(error_count):
    return (
        "V C S   S i m u l a t i o n   R e p o r t\n"
        "Time: 1885000 ps\n"
        "--- UVM Report Summary ---\n"
        "UVM_INFO : 12\n"
        "UVM_WARNING : 0\n"
        "UVM_ERROR : %d\n"
        "UVM_FATAL : 0\n" % error_count
    )


def test_cache_layout_resolves_per_case_and_shared_artifacts(tmp_path):
    project_root = tmp_path / "xvp_3p_cache"
    scan_root = project_root / "run" / "test"
    shared_daidir = project_root / "run" / "build" / "simv.daidir"
    shared_kdb = shared_daidir / "kdb.elab++"
    shared_kdb.mkdir(parents=True)
    config_path = _artifact_config(project_root)

    for index, case_name in enumerate(CASE_NAMES):
        case_dir = scan_root / case_name
        case_dir.mkdir(parents=True)
        log_path = case_dir / (case_name + ".log")
        fsdb_path = case_dir / (case_name + ".fsdb")
        manifest_path = case_dir / "xvp_case_manifest.json"
        fsdb_path.write_bytes(b"fsdb")
        manifest_path.write_text('{"schema":"xvp_case_manifest.v1"}\n', encoding="utf-8")
        artifact_line = "XVP_ARTIFACT FSDB=%s DAIDIR=%s RUN_MANIFEST=%s\n" % (
            fsdb_path,
            shared_daidir,
            manifest_path,
        )
        if case_name == "baseline_clean_1":
            content = "UVM_INFO @ 1885000ps: reporter [3P_CACHE_MAIN_PASS] completed\n" + artifact_line + _summary_lines(0)
        else:
            content = (
                "UVM_ERROR ../test/xvp_3p_cache_user_tc_lib.sv(155) @ %dps: reporter "
                "[3P_CACHE_CHECKER] mismatch expected data=0x%02x actual=0x%02x\n"
                % (345000 + index, 32 + index, 33 + index)
            ) + artifact_line + _summary_lines(1)
        log_path.write_text(content, encoding="utf-8")

    bundle = _scan(scan_root, config_path, tmp_path / "output" / "xlog_bundle.json")

    assert validate_instance(bundle, SCHEMA) is True
    assert bundle["api_version"] == "xlog_bundle.v1"
    assert bundle["schema_revision"] == "1.3"
    assert bundle["summary"]["cases_total"] == 5
    assert bundle["summary"]["cases_passed"] == 1
    assert bundle["summary"]["cases_failed"] == 4
    assert bundle["summary"]["failure_clusters"] == 1
    assert [case["case_id"] for case in bundle["cases"]] == sorted(
        "%s/%s.log" % (name, name) for name in CASE_NAMES
    )

    fsdb_paths = set()
    daidir_paths = set()
    kdb_paths = set()
    for case in bundle["cases"]:
        resources = case["artifacts"]["resources"]
        assert resources["log"]["status"] == "resolved"
        assert resources["fsdb"]["status"] == "resolved"
        assert resources["daidir"]["status"] == "resolved"
        assert resources["kdb"]["status"] == "resolved"
        assert resources["run_manifest"]["status"] == "resolved"
        assert case["artifacts"]["manifests"]["selection_status"] == "legacy_fallback"
        assert case["artifacts"]["manifests"]["selected"]["artifact_kind"] == "xvp.case_manifest"
        assert "run_manifest" not in case["artifacts"]["xdebug_target"]
        assert Path(resources["fsdb"]["selected"]["path"]).parent.name == case["test_id"] + "_1"
        fsdb_paths.add(resources["fsdb"]["selected"]["path"])
        daidir_paths.add(resources["daidir"]["selected"]["path"])
        kdb_paths.add(resources["kdb"]["selected"]["path"])
        for kind in ("fsdb", "daidir", "run_manifest"):
            assert all("=" not in candidate["path"] for candidate in resources[kind]["candidates"])

    assert len(fsdb_paths) == 5
    assert daidir_paths == {str(shared_daidir)}
    assert kdb_paths == {str(shared_kdb)}
    cluster = bundle["failure_clusters"][0]
    assert len(cluster["case_ids"]) == 4
    assert cluster["recommendation"]["recommended_case_id"] in cluster["case_ids"]
    assert len(cluster["recommendation"]["alternate_case_ids"]) == 3


def test_cache_layout_reports_missing_and_ambiguous_artifacts_without_searching(tmp_path):
    project_root = tmp_path / "xvp_3p_cache"
    scan_root = project_root / "run" / "test"
    case_dir = scan_root / "ambiguous_wave_1"
    shared_daidir = project_root / "run" / "build" / "simv.daidir"
    case_dir.mkdir(parents=True)
    shared_daidir.mkdir(parents=True)
    first = case_dir / "first.fsdb"
    second = case_dir / "second.fsdb"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    decoy_kdb = project_root / "unconfigured" / "simv.daidir" / "kdb.elab++"
    decoy_kdb.mkdir(parents=True)
    log_path = case_dir / "ambiguous_wave_1.log"
    log_path.write_text(
        "UVM_ERROR /tb/dut.sv(9) @ 1ns: reporter [E] failure\n"
        "XVP_ARTIFACT FSDB=%s FSDB=%s DAIDIR=%s\n" % (first, second, shared_daidir),
        encoding="utf-8",
    )

    bundle = _scan(scan_root, _artifact_config(project_root), tmp_path / "bundle.json")
    resources = bundle["cases"][0]["artifacts"]["resources"]

    assert resources["fsdb"]["status"] == "ambiguous"
    assert resources["fsdb"]["reason"] == "multiple_best_candidates"
    assert resources["daidir"]["selected"]["path"] == str(shared_daidir)
    assert resources["kdb"]["status"] == "unavailable"
    assert resources["kdb"]["reason"] == "no_existing_candidates"
    assert str(decoy_kdb) not in [candidate["path"] for candidate in resources["kdb"]["candidates"]]
