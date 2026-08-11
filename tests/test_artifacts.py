from pathlib import Path

from xlog.artifacts import DEFAULT_ARTIFACT_RULES, build_case_artifacts
from xlog.config import load_effective_scan_config


def _rules(**overrides):
    rules = dict((key, list(value) if isinstance(value, list) else value) for key, value in DEFAULT_ARTIFACT_RULES.items())
    rules.update(overrides)
    return rules


def _identity(test_id="alpha"):
    return {"test_id": test_id, "seed": 1, "seed_parse_status": "parsed"}


def test_artifacts_resolve_xvp_style_case_files(tmp_path):
    root = tmp_path / "regression"
    case_dir = root / "run"
    case_dir.mkdir(parents=True)
    log_path = case_dir / "sim_alpha.log"
    log_path.write_text("UVM_ERROR /tb/dut.sv(1) @ 1ns: reporter [E] failure\n", encoding="utf-8")
    (case_dir / "sim_alpha.fsdb").write_bytes(b"fsdb")
    (case_dir / "simv.daidir").mkdir()
    (case_dir / "simv.daidir" / "kdb").mkdir()

    artifacts = build_case_artifacts(log_path, root, _identity(), _rules())

    assert artifacts["status"] == "complete"
    assert artifacts["resources"]["fsdb"]["selected"]["path"] == str(case_dir / "sim_alpha.fsdb")
    assert artifacts["resources"]["daidir"]["selected"]["path"] == str(case_dir / "simv.daidir")
    assert artifacts["resources"]["kdb"]["selected"]["source"] == "resolved_daidir"
    assert artifacts["xdebug_target"] == {
        "fsdb": str(case_dir / "sim_alpha.fsdb"),
        "daidir": str(case_dir / "simv.daidir"),
    }


def test_artifacts_prioritize_log_reference_and_record_ambiguity(tmp_path):
    root = tmp_path / "regression"
    root.mkdir()
    log_path = root / "case.log"
    first = root / "first.fsdb"
    second = root / "second.fsdb"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    log_path.write_text("opened first.fsdb and second.fsdb\n", encoding="utf-8")

    artifacts = build_case_artifacts(log_path, root, _identity("case"), _rules())

    assert artifacts["resources"]["fsdb"]["status"] == "ambiguous"
    assert artifacts["resources"]["fsdb"]["reason"] == "multiple_best_candidates"
    assert artifacts["xdebug_target"] == {}


def test_artifact_log_reference_strips_assignment_labels(tmp_path):
    root = tmp_path / "regression"
    root.mkdir()
    log_path = root / "case.log"
    fsdb = root / "case.fsdb"
    daidir = root / "simv.daidir"
    fsdb.write_bytes(b"wave")
    daidir.mkdir()
    log_path.write_text(
        "XVP_ARTIFACT FSDB=%s DAIDIR=%s\n" % (fsdb, daidir),
        encoding="utf-8",
    )

    artifacts = build_case_artifacts(log_path, root, _identity("case"), _rules())

    assert artifacts["resources"]["fsdb"]["selected"]["path"] == str(fsdb)
    assert artifacts["resources"]["daidir"]["selected"]["path"] == str(daidir)
    for kind in ("fsdb", "daidir"):
        assert all("=" not in candidate["path"] for candidate in artifacts["resources"][kind]["candidates"])


def test_artifact_templates_adapt_nonstandard_project_layout(tmp_path):
    root = tmp_path / "regression"
    case_dir = root / "logs"
    waves = root / "waves"
    case_dir.mkdir(parents=True)
    waves.mkdir()
    log_path = case_dir / "case_1.log"
    log_path.write_text("log\n", encoding="utf-8")
    (waves / "case.fsdb").write_bytes(b"fsdb")

    artifacts = build_case_artifacts(
        log_path,
        root,
        _identity("case"),
        _rules(log_reference_extraction=False, fsdb_templates=["{regression_root}/waves/{test_id}.fsdb"], daidir_templates=[]),
    )

    assert artifacts["resources"]["fsdb"]["status"] == "resolved"
    assert artifacts["resources"]["fsdb"]["selected"]["path"] == str(waves / "case.fsdb")
    assert artifacts["status"] == "partial"


def test_scan_config_keeps_parser_and_artifact_contracts_separate(tmp_path):
    config_path = tmp_path / "scan.json"
    config_path.write_text(
        '{"parser":{"pass_patterns":["DONE"]},"artifacts":{"fsdb_templates":["{log_dir}/wave.fsdb"]}}',
        encoding="utf-8",
    )

    effective = load_effective_scan_config(str(config_path))

    assert effective["parser"]["pass_patterns"] == ["DONE"]
    assert effective["artifacts"]["fsdb_templates"] == ["{log_dir}/wave.fsdb"]
    assert effective["artifacts"]["xdebug_run_manifest_templates"] == [
        "{log_dir}/xdebug.run-manifest.v1.json"
    ]
