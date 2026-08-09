import hashlib
import json
import re


_SV_NUMBER_RE = re.compile(r"(?<![A-Za-z_])[-+]?(?:\d+)?'s?[bodh][0-9a-f_xz?]+", re.IGNORECASE)
_HEX_NUMBER_RE = re.compile(r"(?<![A-Za-z_])[-+]?0x[0-9a-f_]+", re.IGNORECASE)
_BARE_HEX_RE = re.compile(r"(?<![A-Za-z0-9_])(?=[0-9a-f_]*[a-f])(?=[0-9a-f_]*(?:\d|_))[0-9a-f_]{2,}(?![A-Za-z0-9_])", re.IGNORECASE)
_DECIMAL_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9_])[-+]?\d[\d_]*(?:\.\d[\d_]*)?(?=$|[^A-Za-z0-9_]|(?:fs|ps|ns|us|ms|s)\b)", re.IGNORECASE)
_ANGLE_DYNAMIC_FIELD_RE = re.compile(
    r"<(?P<name>[A-Za-z_]+)(?P<value>(?:[-+]?\d+(?:\.\d+)?)|(?:'s?[bodh][0-9a-f_xz?]+)|(?:[0-9a-f_]*\d[0-9a-f_]*))>",
    re.IGNORECASE,
)
_PATH_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"(?:(?:[A-Za-z]:[\\/])|/|(?:\.\.?[\\/])+)"
    r"(?:[^\s'\"<>,:()]+[\\/])*"
    r"[^\s'\"<>,:()]+\.(?:svh?|vh?|c|cc|cpp|h|hpp|py)"
    r"(?:(?:\(\d+\))|(?::\d+(?::\d+)?))?",
    re.IGNORECASE | re.VERBOSE,
)
_SHARED_PRODUCERS = frozenset(("vcs", "xcelium"))
_ENVIRONMENT_PRODUCERS = frozenset(("uvm", "sva", "configured"))


def description_signature(description, normalize_paths=False):
    normalized = " ".join(str(description or "").split()).lower()
    if normalize_paths:
        normalized = _PATH_TOKEN_RE.sub("<path>", normalized)
    normalized = _ANGLE_DYNAMIC_FIELD_RE.sub(lambda match: "<%s<num>>" % match.group("name"), normalized)
    normalized = _SV_NUMBER_RE.sub("<num>", normalized)
    normalized = _HEX_NUMBER_RE.sub("<num>", normalized)
    normalized = _BARE_HEX_RE.sub("<num>", normalized)
    return _DECIMAL_NUMBER_RE.sub("<num>", normalized)


def scope_hint(error):
    """Return a deterministic clue, never a final knowledge-routing decision."""
    producer = str(error.get("producer", "unknown") or "unknown").strip().lower()
    error_type = str(error.get("error_type", "UNKNOWN") or "UNKNOWN").strip().upper()
    if producer in _SHARED_PRODUCERS:
        candidate = "shared_public"
    elif producer in _ENVIRONMENT_PRODUCERS:
        candidate = "environment_private"
    else:
        candidate = "unknown"
    return {
        "candidate": candidate,
        "status": "non_authoritative",
        "producer": producer,
        "basis": ["producer=%s" % producer, "error_type=%s" % error_type],
        "final_routing": "undetermined",
    }


def portable_error_signature(error):
    hint = error.get("scope_hint") or scope_hint(error)
    template = description_signature(error.get("description", ""), normalize_paths=True)
    parts = {
        "level": str(error.get("level", "") or "").strip().upper(),
        "error_id": str(error.get("error_id", "") or "").strip().lower(),
        "producer": str(hint.get("producer", "unknown") or "unknown").strip().lower(),
        "description_template": template,
    }
    fingerprint, _digest = _fingerprint(parts)
    return {
        "strategy": "level_error_id_producer_description.v1",
        "description_template": template,
        "fingerprint": fingerprint,
    }


def _location_identity(error):
    source_location = error.get("source_location") or {}
    path = str(source_location.get("path", "") or "").strip()
    line = source_location.get("line")
    if path and isinstance(line, int):
        return "%s(%d)" % (path, line)
    return str(error.get("location", "") or "").strip()


def _description_template(error):
    template = error.get("description_template")
    if error.get("description_template_status") == "present" and isinstance(template, str):
        return template
    hint = error.get("scope_hint") or scope_hint(error)
    return description_signature(
        error.get("description", ""),
        normalize_paths=hint.get("candidate") == "shared_public",
    )


def _identity_parts(error):
    return {
        "level": str(error.get("level", "") or "").strip().upper(),
        "error_id": str(error.get("error_id", "") or "").strip().lower(),
        "location": _location_identity(error).lower(),
        "description_template": _description_template(error),
    }


def error_identity(error):
    parts = _identity_parts(error)
    hint = error.get("scope_hint") or scope_hint(error)
    if hint.get("candidate") == "shared_public":
        producer = str(hint.get("producer", "unknown") or "unknown").strip().lower()
        key = (
            parts["level"],
            parts["error_id"],
            producer,
            parts["description_template"],
        )
        return key, "level_error_id_producer_portable_description", parts["description_template"]
    key = (parts["level"], parts["error_id"], parts["location"], parts["description_template"])
    return key, "level_error_id_location_description", parts["description_template"]


def _fingerprint(parts):
    canonical = json.dumps(parts, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return "sha256:" + digest, digest


def build_failure_clusters(cases):
    groups = {}
    unclustered = []
    for case in cases:
        if case.get("status") != "fail":
            continue
        error = case.get("primary_error")
        if not error:
            unclustered.append(case["case_id"])
            continue
        key, strategy, template = error_identity(error)
        parts = _identity_parts(error)
        if strategy == "level_error_id_producer_portable_description":
            parts = dict((name, value) for name, value in parts.items() if name != "location")
            parts["producer"] = (error.get("scope_hint") or scope_hint(error)).get("producer", "unknown")
        groups.setdefault(key, {"strategy": strategy, "template": template, "parts": parts, "items": []})["items"].append((case, error))
    clusters = []
    for key in sorted(groups, key=repr):
        group = groups[key]
        items = sorted(group["items"], key=lambda item: item[0]["case_id"])
        representative_case, representative_error = items[0]
        fingerprint, digest = _fingerprint(group["parts"])
        clusters.append({
            "cluster_id": "error-" + digest,
            "fingerprint": fingerprint,
            "signature": {
                "strategy": group["strategy"],
                "level": str(representative_error.get("level", "") or "").strip().upper(),
                "error_id": str(representative_error.get("error_id", "") or "").strip(),
                "location": str(representative_error.get("location", "") or "").strip(),
                "source_location": representative_error.get("source_location"),
                "description_template": group["template"],
                "scope_hint": representative_error.get("scope_hint") or scope_hint(representative_error),
                "portable_signature": representative_error.get("portable_signature") or portable_error_signature(representative_error),
                "fingerprint": fingerprint,
            },
            "representative_case_id": representative_case["case_id"],
            "representative_error": representative_error,
            "case_ids": [case["case_id"] for case, _ in items],
            "log_paths": [case["log_path"] for case, _ in items],
            "occurrence_count": len(items),
        })
    return clusters, sorted(unclustered)
