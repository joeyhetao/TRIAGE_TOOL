import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .artifacts import build_case_artifacts
from .dedup import build_failure_clusters
from .discovery import discover_log_files
from .errors import XlogError
from .parser import parse_logs
from .recommendation import annotate_case_identity, build_debug_recommendation


def _utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def scan_regression(regression_root, parser_config, artifact_config, max_log_files, workers, debug_budget):
    root, log_files = discover_log_files(regression_root, max_log_files)
    parsed = parse_logs([str(path) for path in log_files], extra_keywords=parser_config["extra_patterns"], pass_patterns=parser_config["pass_patterns"], workers=workers)
    cases = []
    for path, result in zip(log_files, parsed):
        relative_path = path.relative_to(root).as_posix()
        case = {
            "case_id": relative_path,
            "relative_log_path": relative_path,
            "log_path": str(path),
            "working_directory": str(path.parent),
            "status": result["status"],
            "pass_found": bool(result.get("pass_found")),
            "statistics": result.get("statistics", {}),
            "top_errors": result.get("top_errors", []),
            "primary_error": result.get("primary_error"),
            "simulation_time": result.get("simulation_time"),
        }
        annotate_case_identity(case)
        case["artifacts"] = build_case_artifacts(path, root, case, artifact_config)
        if result.get("parse_error"):
            case["parse_error"] = result["parse_error"]
        cases.append(case)
    clusters, unclustered = build_failure_clusters(cases)
    debug_recommendation = build_debug_recommendation(clusters, cases, debug_budget)
    summary = {
        "cases_total": len(cases),
        "cases_passed": sum(case["status"] == "pass" for case in cases),
        "cases_failed": sum(case["status"] == "fail" for case in cases),
        "cases_error": sum(case["status"] == "error" for case in cases),
        "failure_clusters": len(clusters),
        "debug_recommended_cases": debug_recommendation["selected_cluster_count"],
        "unclustered_failure_cases": len(unclustered),
        "artifact_cases_complete": sum(case["artifacts"]["status"] == "complete" for case in cases),
        "artifact_cases_partial": sum(case["artifacts"]["status"] == "partial" for case in cases),
        "artifact_cases_unavailable": sum(case["artifacts"]["status"] == "unavailable" for case in cases),
        "artifact_cases_ambiguous": sum(case["artifacts"]["status"] == "ambiguous" for case in cases),
    }
    return {
        "api_version": "xlog_bundle.v1",
        "generated_at": _utc_now(),
        "source": {"regression_root": str(root), "log_suffix": ".log", "max_log_files": max_log_files, "workers": workers},
        "parser_config": parser_config,
        "artifact_config": artifact_config,
        "summary": summary,
        "cases": cases,
        "failure_clusters": clusters,
        "debug_recommendation": debug_recommendation,
        "unclustered_failure_case_ids": unclustered,
    }


def write_bundle(bundle, output_path):
    path = Path(output_path).expanduser()
    if not path.is_absolute():
        raise XlogError("INVALID_REQUEST", "output_path must be absolute")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        fd, temporary_path = tempfile.mkstemp(prefix=".xlog-", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
            os.replace(temporary_path, str(path))
        except Exception:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass
            raise
    except OSError as exc:
        raise XlogError("OUTPUT_WRITE_FAILED", "cannot write bundle", {"reason": str(exc), "output_path": str(path)})
    return str(path.resolve()), hashlib.sha256(text.encode("utf-8")).hexdigest()
