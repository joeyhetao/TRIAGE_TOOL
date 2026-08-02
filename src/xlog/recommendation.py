import posixpath
import re
from decimal import Decimal, InvalidOperation


DEFAULT_DEBUG_BUDGET = 20
_POLICY_VERSION = "deterministic.v2"
_SEED_SUFFIX_RE = re.compile(r"^(?P<test_id>.+)_(?P<seed>\d+)$")


def case_identity_from_case_id(case_id):
    """Derive stable test/seed identity from a relative POSIX log path."""
    filename = posixpath.basename(str(case_id or ""))
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    match = _SEED_SUFFIX_RE.match(stem)
    if match:
        return {
            "test_id": match.group("test_id"),
            "seed": int(match.group("seed")),
            "seed_parse_status": "parsed",
        }
    return {"test_id": stem, "seed": None, "seed_parse_status": "fallback"}


def annotate_case_identity(case):
    case.update(case_identity_from_case_id(case.get("case_id", "")))
    return case


def _severity_rank(level):
    text = str(level or "").upper()
    if "FATAL" in text:
        return 3
    if "ERROR" in text:
        return 2
    if "WARNING" in text:
        return 1
    return 0


def _signature_completeness(signature):
    return sum(1 for name in ("error_id", "location", "description_template") if signature.get(name))


def _cluster_seed_coverage(case_ids, cases_by_id):
    by_test = {}
    distinct_tests = set()
    for case_id in case_ids:
        case = cases_by_id[case_id]
        test_id = case.get("test_id") or ""
        distinct_tests.add(test_id)
        if case.get("seed_parse_status") == "parsed":
            by_test.setdefault(test_id, set()).add(case.get("seed"))
    max_seed_count = max([len(seeds) for seeds in by_test.values()] or [0])
    return len(distinct_tests), max_seed_count, dict((test_id, len(seeds)) for test_id, seeds in by_test.items())


def _simulation_time_value(case):
    simulation_time = case.get("simulation_time") or {}
    if simulation_time.get("source") == "unavailable":
        return None
    normalized_fs = simulation_time.get("normalized_fs")
    if normalized_fs is None:
        return None
    try:
        return Decimal(str(normalized_fs))
    except InvalidOperation:
        return None


def _candidate_sort_key(case, seed_count_by_test):
    has_evidence = bool(case.get("log_path") and case.get("primary_error"))
    simulation_time = _simulation_time_value(case)
    seed = case.get("seed")
    seed_sort = seed if seed is not None else 10 ** 18
    test_id = case.get("test_id") or ""
    return (
        1 if simulation_time is None else 0,
        simulation_time if simulation_time is not None else Decimal("0"),
        0 if has_evidence else 1,
        -seed_count_by_test.get(test_id, 0),
        seed_sort,
        case.get("case_id", ""),
    )


def _cluster_profile(cluster, cases_by_id):
    case_ids = list(cluster.get("case_ids", []))
    signature = cluster.get("signature", {})
    distinct_tests, max_seed_count, seed_count_by_test = _cluster_seed_coverage(case_ids, cases_by_id)
    candidates = sorted((cases_by_id[case_id] for case_id in case_ids), key=lambda case: _candidate_sort_key(case, seed_count_by_test))
    score = {
        "severity_rank": _severity_rank(signature.get("level")),
        "distinct_test_count": distinct_tests,
        "max_same_test_seed_count": max_seed_count,
        "occurrence_count": int(cluster.get("occurrence_count", len(case_ids))),
        "signature_completeness": _signature_completeness(signature),
    }
    return {"score": score, "candidates": candidates}


def _cluster_sort_key(item):
    cluster, profile = item
    score = profile["score"]
    return (
        -score["severity_rank"],
        -score["distinct_test_count"],
        -score["max_same_test_seed_count"],
        -score["occurrence_count"],
        -score["signature_completeness"],
        cluster.get("cluster_id", ""),
    )


def _simulation_time_reason(case):
    simulation_time = (case or {}).get("simulation_time") or {}
    if simulation_time.get("normalized_fs") is None:
        return "simulation_time=unavailable"
    return "simulation_time=%s%s (%s)" % (
        simulation_time.get("value"),
        simulation_time.get("unit"),
        simulation_time.get("source"),
    )


def _reasons(score, candidate):
    reasons = []
    if score["severity_rank"] >= 3:
        reasons.append("fatal severity")
    elif score["severity_rank"] >= 2:
        reasons.append("error severity")
    reasons.append("distinct_tests=%d" % score["distinct_test_count"])
    reasons.append("max_same_test_seeds=%d" % score["max_same_test_seed_count"])
    reasons.append("occurrences=%d" % score["occurrence_count"])
    reasons.append(_simulation_time_reason(candidate))
    return reasons


def _choose_candidate(candidates):
    return candidates[0] if candidates else None


def build_debug_recommendation(clusters, cases, debug_budget):
    cases_by_id = {case["case_id"]: case for case in cases}
    profiled = [(cluster, _cluster_profile(cluster, cases_by_id)) for cluster in clusters]
    profiled.sort(key=_cluster_sort_key)

    selected = []
    deferred = []
    selected_cluster_ids = set()

    for rank, (cluster, profile) in enumerate(profiled, 1):
        default_candidate = _choose_candidate(profile["candidates"])
        cluster["recommendation"] = {
            "rank": rank,
            "selected": False,
            "recommended_case_id": default_candidate.get("case_id") if default_candidate else None,
            "recommended_simulation_time": default_candidate.get("simulation_time") if default_candidate else None,
            "alternate_case_ids": [case.get("case_id") for case in profile["candidates"][1:]],
            "score_components": profile["score"],
            "reasons": _reasons(profile["score"], default_candidate),
        }

    for rank, (cluster, profile) in enumerate(profiled, 1):
        if len(selected) >= debug_budget:
            break
        candidate = _choose_candidate(profile["candidates"])
        if not candidate:
            continue
        alternates = [case.get("case_id") for case in profile["candidates"] if case.get("case_id") != candidate.get("case_id")]
        cluster["recommendation"].update({
            "rank": rank,
            "selected": True,
            "recommended_case_id": candidate.get("case_id"),
            "recommended_simulation_time": candidate.get("simulation_time"),
            "alternate_case_ids": alternates,
            "reasons": _reasons(profile["score"], candidate),
        })
        selected_cluster_ids.add(cluster.get("cluster_id"))
        selected.append({
            "rank": rank,
            "cluster_id": cluster.get("cluster_id"),
            "case_id": candidate.get("case_id"),
            "log_path": candidate.get("log_path"),
            "test_id": candidate.get("test_id"),
            "seed": candidate.get("seed"),
            "seed_parse_status": candidate.get("seed_parse_status"),
            "simulation_time": candidate.get("simulation_time"),
            "score_components": profile["score"],
            "reasons": cluster["recommendation"]["reasons"],
        })

    for cluster, _profile in profiled:
        if cluster.get("cluster_id") not in selected_cluster_ids:
            deferred.append(cluster.get("cluster_id"))

    return {
        "policy_version": _POLICY_VERSION,
        "debug_budget": debug_budget,
        "eligible_cluster_count": len(clusters),
        "selected_cluster_count": len(selected),
        "recommended_debug_cases": selected,
        "deferred_cluster_ids": deferred,
    }
