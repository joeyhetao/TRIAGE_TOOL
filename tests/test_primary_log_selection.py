from collections import Counter
from pathlib import Path

from xlog.bundle import scan_regression
from xlog.config import load_effective_scan_config
from xlog.discovery import discover_log_files
from xlog.parser import parse_log


def _scan(root):
    config = load_effective_scan_config()
    return scan_regression(
        str(root),
        config["parser"],
        config["artifacts"],
        max_log_files=100,
        workers=1,
        debug_budget=20,
    )


def test_real_case_directory_selects_only_primary_log(tmp_path, monkeypatch):
    root = tmp_path / "regression"
    case_dir = root / "rpe_it_hike_tc_rpe_rm_full_872631"
    case_dir.mkdir(parents=True)
    main_log = case_dir / "tc_rpe_rm_full_872631.log"
    backup_log = case_dir / "tc_rpe_rm_full_872631_bk.log"
    novas_log = case_dir / "novas_dump.log"
    tr_db_log = case_dir / "tr_db.log"
    main_log.write_text(
        "UVM_ERROR /tb/rpe_rm.sv(88) @ 100ns: reporter [RPE_RM] queue full\n",
        encoding="utf-8",
    )
    backup_log.write_text("backup must be excluded\n", encoding="utf-8")
    novas_log.write_text("auxiliary novas log\n", encoding="utf-8")
    tr_db_log.write_text("auxiliary transaction database log\n", encoding="utf-8")

    original_open = Path.open
    opens = Counter()

    def guarded_open(path, *args, **kwargs):
        resolved = path.resolve()
        if resolved in (novas_log.resolve(), tr_db_log.resolve(), backup_log.resolve()):
            raise AssertionError("non-primary log must not be opened")
        if path.suffix.lower() == ".log":
            opens[str(resolved)] += 1
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    bundle = _scan(root)

    assert bundle["summary"]["cases_total"] == 1
    assert bundle["summary"]["cases_failed"] == 1
    assert [case["case_id"] for case in bundle["cases"]] == [
        "rpe_it_hike_tc_rpe_rm_full_872631/tc_rpe_rm_full_872631.log"
    ]
    assert bundle["cases"][0]["primary_error"]["error_id"] == "RPE_RM"
    assert opens == Counter({str(main_log.resolve()): 1})


def test_single_log_directory_keeps_its_only_candidate(tmp_path):
    root = tmp_path / "regression"
    case_dir = root / "unrelated_directory_name"
    case_dir.mkdir(parents=True)
    log_path = case_dir / "only_result.log"
    log_path.write_text("JVP TEST PASSED\n", encoding="utf-8")

    _, files = discover_log_files(str(root), max_log_files=10)

    assert files == [log_path.resolve()]


def test_ambiguous_multi_log_directories_preserve_all_candidates(tmp_path):
    root = tmp_path / "regression"
    no_match = root / "ambiguous"
    no_match.mkdir(parents=True)
    zero_match_logs = [no_match / "alpha.log", no_match / "beta.log"]
    for path in zero_match_logs:
        path.write_text("JVP TEST PASSED\n", encoding="utf-8")

    multiple_matches = root / "prefix_case"
    multiple_matches.mkdir()
    multi_match_logs = [
        multiple_matches / "case.log",
        multiple_matches / "prefix_case.log",
    ]
    for path in multi_match_logs:
        path.write_text("JVP TEST PASSED\n", encoding="utf-8")

    _, files = discover_log_files(str(root), max_log_files=10)

    expected = sorted(
        [path.resolve() for path in zero_match_logs + multi_match_logs],
        key=lambda path: path.relative_to(root.resolve()).as_posix(),
    )
    assert files == expected


def test_primary_log_match_is_case_insensitive(tmp_path):
    root = tmp_path / "regression"
    case_dir = root / "RPE_IT_HIKE_TC_MiXeD_9"
    case_dir.mkdir(parents=True)
    main_log = case_dir / "Tc_MiXeD_9.LOG"
    main_log.write_text("JVP TEST PASSED\n", encoding="utf-8")
    (case_dir / "NOVAS_DUMP.LOG").write_text("auxiliary\n", encoding="utf-8")
    (case_dir / "TR_DB.log").write_text("auxiliary\n", encoding="utf-8")

    _, files = discover_log_files(str(root), max_log_files=10)

    assert files == [main_log.resolve()]


def test_large_log_scans_middle_error_and_tail_completion_facts(tmp_path):
    log_path = tmp_path / "large_case.log"
    with log_path.open("w", encoding="utf-8") as handle:
        for index in range(25000):
            handle.write("ordinary simulator output before error %d\n" % index)
        handle.write(
            "UVM_ERROR /tb/dut.sv(77) @ 500ns: reporter [MID_ERROR] "
            "mismatch in the middle\n"
        )
        for index in range(25000):
            handle.write("ordinary simulator output after error %d\n" % index)
        handle.write("V C S   S i m u l a t i o n   R e p o r t\n")
        handle.write("Time: 2us\n")
        handle.write("JVP TEST PASSED\n")

    result = parse_log(
        str(log_path),
        pass_patterns=["JVP TEST PASSED"],
    )

    assert result["status"] == "fail"
    assert result["pass_found"] is True
    assert result["primary_error"]["error_id"] == "MID_ERROR"
    assert result["simulation_time"] == {
        "value": "2",
        "unit": "us",
        "normalized_fs": "2000000000",
        "source": "explicit_end_marker",
    }
