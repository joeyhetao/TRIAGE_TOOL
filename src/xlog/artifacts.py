import json
import os
import re
from pathlib import Path


ARTIFACT_KINDS = ("log", "fsdb", "daidir", "kdb", "run_manifest")
XDEBUG_MANIFEST_KIND = "xdebug.run_manifest"
XDEBUG_MANIFEST_SCHEMA = "xdebug.run-manifest.v1"
XVP_MANIFEST_KIND = "xvp.case_manifest"
XVP_MANIFEST_SCHEMA = "xvp_case_manifest.v1"
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
    "xdebug_run_manifest_templates": ["{log_dir}/xdebug.run-manifest.v1.json"],
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
    "run_manifest": re.compile(r"(?P<path>[^\s'\"<>]*xvp_case_manifest[^\s'\"<>]*\.json)\b", re.IGNORECASE),
    "xdebug_run_manifest": re.compile(
        r"(?P<path>[^\s'\"<>]*xdebug[._-]run[._-]manifest[^\s'\"<>]*\.json)\b",
        re.IGNORECASE,
    ),
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


def _manifest_descriptor(artifact_kind, expected_schema, schema_field, resource, required_state=None):
    candidates = resource["candidates"]
    selected = resource.get("selected")
    path = selected["path"] if selected else (candidates[0]["path"] if len(candidates) == 1 else None)
    descriptor = {
        "artifact_kind": artifact_kind,
        "expected_schema_version": expected_schema,
        "schema_version": None,
        "path": path,
        "resolution_status": resource["status"],
        "parse_status": "not_parsed",
        "document_state": None,
        "reason": resource["reason"],
        "candidates": candidates,
    }
    if resource["status"] != "resolved":
        return descriptor, None

    try:
        with Path(selected["path"]).open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except OSError:
        descriptor["parse_status"] = "read_error"
        descriptor["reason"] = "manifest_read_failed"
        return descriptor, None
    except ValueError:
        descriptor["parse_status"] = "invalid_json"
        descriptor["reason"] = "manifest_invalid_json"
        return descriptor, None

    if not isinstance(payload, dict):
        descriptor["parse_status"] = "invalid_root"
        descriptor["reason"] = "manifest_root_not_object"
        return descriptor, None

    schema_version = payload.get(schema_field)
    if isinstance(schema_version, str):
        descriptor["schema_version"] = schema_version
    state = payload.get("state")
    if isinstance(state, str):
        descriptor["document_state"] = state
    if schema_version != expected_schema:
        descriptor["parse_status"] = "schema_mismatch"
        descriptor["reason"] = "declared_schema_mismatch"
        return descriptor, payload
    if required_state is not None and state != required_state:
        descriptor["parse_status"] = "invalid_state"
        descriptor["reason"] = "manifest_not_%s" % required_state
        return descriptor, payload

    descriptor["parse_status"] = "parsed"
    descriptor["reason"] = None
    return descriptor, payload


def _xdebug_external_references(xvp_payload, xvp_path):
    if not isinstance(xvp_payload, dict) or not xvp_path:
        return []
    references = []
    for item in xvp_payload.get("external_manifests", []):
        if (
            not isinstance(item, dict)
            or item.get("kind") != XDEBUG_MANIFEST_KIND
            or item.get("schema_version") != XDEBUG_MANIFEST_SCHEMA
        ):
            continue
        value = item.get("path")
        if isinstance(value, str) and value.strip():
            references.append(_absolute_path(value.strip(), Path(xvp_path).parent))
    return references


def _manifest_snapshot(resources, log_references, rules, context, log_directory):
    xvp_descriptor, xvp_payload = _manifest_descriptor(
        XVP_MANIFEST_KIND,
        XVP_MANIFEST_SCHEMA,
        "schema",
        resources["run_manifest"],
    )
    if xvp_descriptor["parse_status"] != "parsed":
        xvp_payload = None

    candidates = []
    for value in log_references.get("xdebug_run_manifest", []):
        _append_candidate(candidates, _absolute_path(value, log_directory), "log_reference", 0, "file")
    for path in _xdebug_external_references(xvp_payload, xvp_descriptor["path"]):
        _append_candidate(candidates, path, "xvp_external_manifest", 5, "file")
    for index, path in enumerate(
        _expand_templates(rules.get("xdebug_run_manifest_templates", []), context, log_directory)
    ):
        _append_candidate(candidates, path, "configured_template", 10 + index, "file")
    candidates.sort(key=lambda item: (item["priority"], item["path"]))
    xdebug_resource = _resource("xdebug_run_manifest", candidates)
    xdebug_descriptor, _ = _manifest_descriptor(
        XDEBUG_MANIFEST_KIND,
        XDEBUG_MANIFEST_SCHEMA,
        "schema_version",
        xdebug_resource,
        required_state="published",
    )

    if xdebug_descriptor["parse_status"] == "parsed":
        selection_status = "preferred"
        selected = xdebug_descriptor
    elif xvp_descriptor["parse_status"] == "parsed":
        selection_status = "legacy_fallback"
        selected = xvp_descriptor
    else:
        selection_status = "unavailable"
        selected = None
    return {
        "preferred_kind": XDEBUG_MANIFEST_KIND,
        "selection_status": selection_status,
        "selected": selected,
        "items": [xdebug_descriptor, xvp_descriptor],
    }


def _xdebug_target(resources, manifests):
    target = {}
    if resources["fsdb"]["status"] == "resolved":
        target["fsdb"] = resources["fsdb"]["selected"]["path"]
    if resources["daidir"]["status"] == "resolved":
        target["daidir"] = resources["daidir"]["selected"]["path"]
    selected_manifest = manifests.get("selected")
    if (
        target.get("fsdb")
        and manifests.get("selection_status") == "preferred"
        and selected_manifest
        and selected_manifest["artifact_kind"] == XDEBUG_MANIFEST_KIND
    ):
        target["run_manifest"] = selected_manifest["path"]
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
    manifests = _manifest_snapshot(resources, log_references, rules, context, log_directory)
    return {
        "status": _artifact_status(resources),
        "case_directory": str(log_directory),
        "resources": resources,
        "manifests": manifests,
        "xdebug_target": _xdebug_target(resources, manifests),
    }
