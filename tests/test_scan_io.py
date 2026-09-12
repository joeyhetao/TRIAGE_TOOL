import os
import threading
import time
from collections import Counter
from pathlib import Path

import xlog.artifacts as artifacts_module
import xlog.bundle as bundle_module
import xlog.parser as parser_module
from xlog.artifacts import DEFAULT_ARTIFACT_RULES, build_case_artifacts, new_log_references
from xlog.bundle import scan_regression
from xlog.config import load_effective_scan_config
from xlog.discovery import discover_log_files
from xlog.parser import parse_log


FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "rtl_injection_minimal" / "regression"


def _scan(root, workers=4):
    config = load_effective_scan_config()
    return scan_regression(
        str(root),
        config["parser"],
        config["artifacts"],
        max_log_files=2000,
        workers=workers,
        debug_budget=20,
    )


def _identity(test_id="case"):
    return {"test_id": test_id, "seed": 1, "seed_parse_status": "parsed"}


def _tracked_open(monkeypatch):
    original_open = Path.open
    counts = Counter()
    lock = threading.Lock()

    def tracking_open(path, *args, **kwargs):
        suffix = path.suffix.lower()
        if suffix == ".fsdb":
            raise AssertionError("xlog must not open FSDB content")
        if suffix == ".log":
            with lock:
                counts[str(path.resolve())] += 1
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", tracking_open)
    return counts


def test_discovery_filters_non_logs_before_file_validation(tmp_path, monkeypatch):
    root = tmp_path / "regression"
    root.mkdir()
    log_path = root / "case.log"
    log_path.write_text("JVP TEST PASSED\n", encoding="utf-8")
    fsdb_path = root / "large.fsdb"
    fsdb_path.touch()
    os.truncate(str(fsdb_path), 4 * 1024 ** 3)

    original_is_file = Path.is_file
    original_stat = Path.stat

    def guarded_is_file(path):
        if path == fsdb_path:
            raise AssertionError("non-log artifact reached is_file")
        return original_is_file(path)

    def guarded_stat(path, *args, **kwargs):
        if path == fsdb_path:
            raise AssertionError("non-log artifact reached Path.stat")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "is_file", guarded_is_file)
    monkeypatch.setattr(Path, "stat", guarded_stat)

    _, files = discover_log_files(str(root), max_log_files=10)

    assert files == [log_path.resolve()]


def test_parser_collects_artifact_references_during_log_read(tmp_path):
    log_path = tmp_path / "case.log"
    log_path.write_text(
        "JVP TEST PASSED\n"
        "XVP_ARTIFACT FSDB=waves.fsdb DAIDIR=simv.daidir "
        "RUN_MANIFEST=xvp_case_manifest.json\n"
        "XDEBUG_RUN_MANIFEST=xdebug.run-manifest.v1.json\n",
        encoding="utf-8",
    )

    result = parse_log(str(log_path), pass_patterns=["JVP TEST PASSED"])

    assert result["status"] == "pass"
    assert result["artifact_references"]["fsdb"] == ["waves.fsdb"]
    assert result["artifact_references"]["daidir"] == ["simv.daidir"]
    assert result["artifact_references"]["run_manifest"] == ["xvp_case_manifest.json"]
    assert result["artifact_references"]["xdebug_run_manifest"] == [
        "xdebug.run-manifest.v1.json"
    ]


def test_supplied_empty_references_do_not_fall_back_to_log_read(tmp_path, monkeypatch):
    root = tmp_path / "regression"
    root.mkdir()
    log_path = root / "case.log"
    log_path.write_text("JVP TEST PASSED\n", encoding="utf-8")

    def unexpected_read(_):
        raise AssertionError("already-scanned empty references must not reread the log")

    monkeypatch.setattr(artifacts_module, "_log_reference_paths", unexpected_read)

    artifacts = build_case_artifacts(
        log_path,
        root,
        _identity(),
        DEFAULT_ARTIFACT_RULES,
        log_references=new_log_references(),
    )

    assert artifacts["resources"]["log"]["status"] == "resolved"


def test_scan_reads_each_log_once_and_never_opens_fsdb(tmp_path, monkeypatch):
    root = tmp_path / "regression"
    root.mkdir()
    log_paths = []
    for index in range(3):
        log_path = root / ("case_%d.log" % index)
        fsdb_path = root / ("case_%d.fsdb" % index)
        fsdb_path.write_bytes(b"wave")
        log_path.write_text(
            "JVP TEST PASSED\nXVP_ARTIFACT FSDB=%s\n" % fsdb_path,
            encoding="utf-8",
        )
        log_paths.append(log_path)

    counts = _tracked_open(monkeypatch)
    bundle = _scan(root, workers=3)

    assert bundle["summary"]["cases_total"] == 3
    assert bundle["summary"]["cases_passed"] == 3
    assert counts == Counter(str(path.resolve()) for path in log_paths)
    assert all(
        case["artifacts"]["resources"]["fsdb"]["status"] == "resolved"
        for case in bundle["cases"]
    )


def test_parse_failure_keeps_structured_status_and_artifact_fallback(tmp_path, monkeypatch):
    root = tmp_path / "regression"
    root.mkdir()
    log_path = root / "case_1.log"
    fsdb_path = root / "case_1.fsdb"
    fsdb_path.write_bytes(b"wave")
    log_path.write_text("XVP_ARTIFACT FSDB=%s\n" % fsdb_path, encoding="utf-8")

    def fail_parse(*_args, **_kwargs):
        raise RuntimeError("synthetic parser failure")

    monkeypatch.setattr(parser_module, "parse_log", fail_parse)
    bundle = _scan(root, workers=1)
    case = bundle["cases"][0]

    assert case["status"] == "error"
    assert case["parse_error"]["code"] == "LOG_READ_FAILED"
    assert case["simulation_time"]["source"] == "unavailable"
    assert case["artifacts"]["resources"]["fsdb"]["status"] == "resolved"


def test_optimized_and_fallback_scans_have_equal_bundle_semantics(tmp_path, monkeypatch):
    root = tmp_path / "regression"
    root.mkdir()
    fsdb_path = root / "failed_7.fsdb"
    fsdb_path.write_bytes(b"wave")
    (root / "failed_7.log").write_text(
        "UVM_ERROR /tb/dut.sv(9) @ 10ns: reporter [E] mismatch value=7fc\n"
        "XVP_ARTIFACT FSDB=%s\n" % fsdb_path,
        encoding="utf-8",
    )
    (root / "passed_8.log").write_text("JVP TEST PASSED\n", encoding="utf-8")

    optimized = _scan(root, workers=2)
    original_parse_logs = bundle_module.parse_logs

    def parse_without_handoff(*args, **kwargs):
        results = original_parse_logs(*args, **kwargs)
        for result in results:
            result.pop("artifact_references", None)
        return results

    monkeypatch.setattr(bundle_module, "parse_logs", parse_without_handoff)
    fallback = _scan(root, workers=2)

    optimized["generated_at"] = "<dynamic>"
    fallback["generated_at"] = "<dynamic>"
    assert optimized == fallback


def test_repository_fixture_matches_fallback_scan_semantics(monkeypatch):
    optimized = _scan(FIXTURE_ROOT, workers=4)
    original_parse_logs = bundle_module.parse_logs

    def parse_without_handoff(*args, **kwargs):
        results = original_parse_logs(*args, **kwargs)
        for result in results:
            result.pop("artifact_references", None)
        return results

    monkeypatch.setattr(bundle_module, "parse_logs", parse_without_handoff)
    fallback = _scan(FIXTURE_ROOT, workers=4)

    optimized["generated_at"] = "<dynamic>"
    fallback["generated_at"] = "<dynamic>"
    assert optimized == fallback
    assert optimized["api_version"] == "xlog_bundle.v1"
    assert optimized["schema_revision"] == "1.3"
    assert optimized["summary"]["failure_clusters"] == 3


def test_large_regression_with_sparse_fsdb_is_bounded(tmp_path, monkeypatch):
    case_count = 1550
    sparse_size = 4 * 1024 ** 3
    root = tmp_path / "regression"
    root.mkdir()
    log_paths = []
    for index in range(case_count):
        stem = "case_%04d" % index
        log_path = root / (stem + ".log")
        fsdb_path = root / (stem + ".fsdb")
        log_path.write_text("JVP TEST PASSED\n", encoding="utf-8")
        fsdb_path.touch()
        os.truncate(str(fsdb_path), sparse_size)
        log_paths.append(log_path)

    counts = _tracked_open(monkeypatch)
    started = time.monotonic()
    bundle = _scan(root, workers=8)
    elapsed = time.monotonic() - started

    assert bundle["summary"]["cases_total"] == case_count
    assert bundle["summary"]["cases_passed"] == case_count
    assert bundle["summary"]["cases_failed"] == 0
    assert len(counts) == case_count
    assert set(counts.values()) == {1}
    assert all(
        case["artifacts"]["resources"]["fsdb"]["selected"]["size_bytes"] == sparse_size
        for case in bundle["cases"]
    )
    print(
        "large-regression benchmark: cases=%d elapsed=%.3fs log_opens=%d"
        % (case_count, elapsed, sum(counts.values()))
    )
    assert elapsed < 180
