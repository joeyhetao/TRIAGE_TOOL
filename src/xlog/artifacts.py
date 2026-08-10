import os
import re
from pathlib import Path


ARTIFACT_KINDS = ("log", "fsdb", "daidir", "kdb", "run_manifest")
DEFAULT_ARTIFACT_RULES = {
    "log_reference_extraction": True,
    "fsdb_templates": [
        "{log_dir}/{log_stem}.fsdb",
        "{log_dir}/sim_{test_id}.fsdb",
        "{log_dir}/sim.fsdb",
    ],
    "daidir_templates": [
        "{log_dir}/simv.daidir",
        "{regression_root}/simv.daidir",
    ],
    "kdb_templates": [],
    "run_manifest_templates": [],
}

_EXPECTED_KIND = {
    "log": "file",
    "fsdb": "file",
    "daidir": "directory",
    "kdb": "directory",
    "run_manifest": "file",
}
_LOG_REFERENCE_PATTERNS = {
    "fsdb": re.compile(r"(?P<path>[^\s'\"<>]+\.fsdb)\b", re.IGNORECASE),
    "daidir": re.compile(r"(?P<path>[^\s'\"<>]+\.daidir)\b", re.IGNORECASE),
    "kdb": re.compile(r"(?P<path>[^\s'\"<>]+(?:\.kdb|/kdb))\b", re.IGNORECASE),
    "run_manifest": re.compile(r"(?P<path>[^\s'\"<>]*(?:run[_-]?manifest)[^\s'\"<>]*\.json)\b", re.IGNORECASE),
}
_LOG_REFERENCE_ASSIGNMENT = re.compile(r"^\+?[A-Z][A-Z0-9_]*=(?P<path>.+)$", re.IGNORECASE)


def _absolute_path(value, base_directory):
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = Path(base_directory) / path
    return Path(os.path.abspath(os.path.normpath(str(path))))


def _candidate(path, source, priority, expected_kind):
    path = Path(path)
    value = {
        "path": str(path),
        "source": source,
        "priority": priority,
        "expected_kind": expected_kind,
        "exists": False,
        "readable": False,
        "file_type": "missing",
        "size_bytes": None,
        "state": "missing",
    }
    try:
        stat = path.stat()
    except OSError as exc:
        value["reason"] = "not_found" if not path.exists() else "stat_failed"
        if path.exists():
            value["reason"] = "stat_failed:%s" % exc.__class__.__name__
        return value

    value["exists"] = True
    if path.is_file():
        value["file_type"] = "file"
        value["size_bytes"] = stat.st_size
    elif path.is_dir():
        value["file_type"] = "directory"
    else:
        value["file_type"] = "other"

    if value["file_type"] != expected_kind:
        value["state"] = "wrong_type"
        value["reason"] = "expected_%s" % expected_kind
        return value
    value["readable"] = os.access(str(path), os.R_OK)
    if not value["readable"]:
        value["state"] = "unreadable"
        value["reason"] = "permission_denied"
        return value
    value["state"] = "available"
    value["reason"] = None
    return value


def _append_candidate(candidates, path, source, priority, expected_kind):
    value = _candidate(path, source, priority, expected_kind)
    for existing in candidates:
        if existing["path"] == value["path"]:
            if priority < existing["priority"]:
                candidates.remove(existing)
                break
            return
    candidates.append(value)


def _log_reference_paths(log_path):
    references = dict((artifact_kind, []) for artifact_kind in _LOG_REFERENCE_PATTERNS)
    try:
        with Path(log_path).open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                for artifact_kind, pattern in _LOG_REFERENCE_PATTERNS.items():
                    for match in pattern.finditer(line):
                        value = match.group("path")
                        assignment = _LOG_REFERENCE_ASSIGNMENT.match(value)
                        if assignment:
                            value = assignment.group("path")
                        references[artifact_kind].append(value.rstrip(",;"))
    except OSError:
        return references
    return references


def _expand_templates(templates, context, base_directory):
    values = []
    for template in templates:
        try:
            rendered = template.format(**context)
        except (KeyError, ValueError):
            continue
        values.append(_absolute_path(rendered, base_directory))
    return values


def _resource(artifact_kind, candidates):
    available = [candidate for candidate in candidates if candidate["state"] == "available"]
    if available:
        priority = min(candidate["priority"] for candidate in available)
        selected = [candidate for candidate in available if candidate["priority"] == priority]
        if len(selected) == 1:
            return {
                "status": "resolved",
                "selected": selected[0],
                "candidates": candidates,
                "reason": None,
            }
        return {
            "status": "ambiguous",
            "selected": None,
            "candidates": candidates,
            "reason": "multiple_best_candidates",
        }
    if not candidates:
        reason = "no_candidates"
    elif any(candidate["state"] == "unreadable" for candidate in candidates):
        reason = "no_readable_candidates"
    elif any(candidate["state"] == "wrong_type" for candidate in candidates):
        reason = "candidate_type_mismatch"
    else:
        reason = "no_existing_candidates"
    return {
        "status": "unavailable",
        "selected": None,
        "candidates": candidates,
        "reason": reason,
    }


def _artifact_status(resources):
    fsdb = resources["fsdb"]["status"] == "resolved"
    daidir = resources["daidir"]["status"] == "resolved"
    if fsdb and daidir:
        return "complete"
    if fsdb or daidir:
        return "partial"
    if any(resource["status"] == "ambiguous" for resource in resources.values()):
        return "ambiguous"
    return "unavailable"


def _xdebug_target(resources):
    target = {}
    if resources["fsdb"]["status"] == "resolved":
        target["fsdb"] = resources["fsdb"]["selected"]["path"]
    if resources["daidir"]["status"] == "resolved":
        target["daidir"] = resources["daidir"]["selected"]["path"]
    if target.get("fsdb") and resources["run_manifest"]["status"] == "resolved":
        target["run_manifest"] = resources["run_manifest"]["selected"]["path"]
    return target


def build_case_artifacts(log_path, regression_root, identity, rules):
    """Discover a case's debug artifacts without recursive regression searches."""
    log_path = _absolute_path(log_path, regression_root)
    log_directory = log_path.parent
    context = {
        "log_dir": str(log_directory),
        "log_stem": log_path.stem,
        "test_id": identity.get("test_id") or "",
        "regression_root": str(regression_root),
    }
    resources = {}
    log_references = _log_reference_paths(log_path) if rules.get("log_reference_extraction") else {}
    for artifact_kind in ARTIFACT_KINDS:
        expected_kind = _EXPECTED_KIND[artifact_kind]
        candidates = []
        if artifact_kind == "log":
            _append_candidate(candidates, log_path, "discovered_log", 0, expected_kind)
        else:
            if log_references:
                for value in log_references.get(artifact_kind, []):
                    _append_candidate(candidates, _absolute_path(value, log_directory), "log_reference", 0, expected_kind)
            template_key = artifact_kind + "_templates"
            for index, path in enumerate(_expand_templates(rules.get(template_key, []), context, log_directory)):
                _append_candidate(candidates, path, "configured_template", 10 + index, expected_kind)
            if artifact_kind == "kdb" and resources.get("daidir", {}).get("status") == "resolved":
                _append_candidate(candidates, Path(resources["daidir"]["selected"]["path"]) / "kdb", "resolved_daidir", 5, expected_kind)
        candidates.sort(key=lambda item: (item["priority"], item["path"]))
        resources[artifact_kind] = _resource(artifact_kind, candidates)
    return {
        "status": _artifact_status(resources),
        "case_directory": str(log_directory),
        "resources": resources,
        "xdebug_target": _xdebug_target(resources),
    }
