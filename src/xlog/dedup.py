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


def description_signature(description):
    normalized = " ".join(str(description or "").split()).lower()
    normalized = _ANGLE_DYNAMIC_FIELD_RE.sub(lambda match: "<%s<num>>" % match.group("name"), normalized)
    normalized = _SV_NUMBER_RE.sub("<num>", normalized)
    normalized = _HEX_NUMBER_RE.sub("<num>", normalized)
    normalized = _BARE_HEX_RE.sub("<num>", normalized)
    return _DECIMAL_NUMBER_RE.sub("<num>", normalized)


def _location_identity(error):
    source_location = error.get("source_location") or {}
    path = str(source_location.get("path", "") or "").strip()
    line = source_location.get("line")
    if path and isinstance(line, int):
        return "%s(%d)" % (path, line)
    return str(error.get("location", "") or "").strip()


def _identity_parts(error):
    return {
        "level": str(error.get("level", "") or "").strip().upper(),
        "error_id": str(error.get("error_id", "") or "").strip().lower(),
        "location": _location_identity(error).lower(),
        "description_template": description_signature(error.get("description", "")),
    }


def error_identity(error):
    parts = _identity_parts(error)
    key = (
        parts["level"],
        parts["error_id"],
        parts["location"],
        parts["description_template"],
    )
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
                "fingerprint": fingerprint,
            },
            "representative_case_id": representative_case["case_id"],
            "representative_error": representative_error,
            "case_ids": [case["case_id"] for case, _ in items],
            "log_paths": [case["log_path"] for case, _ in items],
            "occurrence_count": len(items),
        })
    return clusters, sorted(unclustered)
