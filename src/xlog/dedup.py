import hashlib
import re


_SV_NUMBER_RE = re.compile(r"(?<![A-Za-z_])[-+]?(?:\d+)?'s?[bodh][0-9a-f_xz?]+", re.IGNORECASE)
_HEX_NUMBER_RE = re.compile(r"(?<![A-Za-z_])[-+]?0x[0-9a-f_]+", re.IGNORECASE)
_BARE_HEX_RE = re.compile(r"(?<![A-Za-z0-9_])(?=[0-9a-f_]*[a-f])(?=[0-9a-f_]*(?:\d|_))[0-9a-f_]{2,}(?![A-Za-z0-9_])", re.IGNORECASE)
_DECIMAL_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9_])[-+]?\d[\d_]*(?:\.\d[\d_]*)?(?=$|[^A-Za-z0-9_]|(?:fs|ps|ns|us|ms|s)\b)", re.IGNORECASE)


def description_signature(description):
    normalized = " ".join(str(description or "").split()).lower()
    normalized = _SV_NUMBER_RE.sub("<num>", normalized)
    normalized = _HEX_NUMBER_RE.sub("<num>", normalized)
    normalized = _BARE_HEX_RE.sub("<num>", normalized)
    return _DECIMAL_NUMBER_RE.sub("<num>", normalized)


def error_identity(error):
    level = str(error.get("level", "") or "").strip().upper()
    error_id = str(error.get("error_id", "") or "").strip()
    location = str(error.get("location", "") or "").strip()
    template = description_signature(error.get("description", ""))
    if error_id and location:
        return (level, error_id.lower(), location.lower()), "level_error_id_location", template
    if error_id:
        return (level, error_id.lower(), template), "level_error_id_description", template
    if location:
        return (level, location.lower(), template), "level_location_description", template
    return (level, template), "level_description", template


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
        groups.setdefault(key, {"strategy": strategy, "template": template, "items": []})["items"].append((case, error))
    clusters = []
    for key in sorted(groups, key=repr):
        group = groups[key]
        items = sorted(group["items"], key=lambda item: item[0]["case_id"])
        representative_case, representative_error = items[0]
        digest = hashlib.sha256(repr(key).encode("utf-8")).hexdigest()
        clusters.append({
            "cluster_id": "error-" + digest,
            "signature": {
                "strategy": group["strategy"],
                "level": str(representative_error.get("level", "") or "").strip().upper(),
                "error_id": str(representative_error.get("error_id", "") or "").strip(),
                "location": str(representative_error.get("location", "") or "").strip(),
                "description_template": group["template"],
            },
            "representative_case_id": representative_case["case_id"],
            "representative_error": representative_error,
            "case_ids": [case["case_id"] for case, _ in items],
            "log_paths": [case["log_path"] for case, _ in items],
            "occurrence_count": len(items),
        })
    return clusters, sorted(unclustered)
