from xlog.recommendation import build_debug_recommendation, case_identity_from_case_id


def _simulation_time(value=None, unit=None, normalized_fs=None, source="unavailable"):
    return {
        "value": value,
        "unit": unit,
        "normalized_fs": normalized_fs,
        "source": source,
    }


def _case(case_id, level="UVM_ERROR", error_id="E", location="dut.sv(1)", simulation_time=None):
    identity = case_identity_from_case_id(case_id)
    case = {
        "case_id": case_id,
        "log_path": "/regress/" + case_id,
        "status": "fail",
        "primary_error": {"level": level, "error_id": error_id, "location": location, "description": "failure"},
        "simulation_time": simulation_time or _simulation_time(),
    }
    case.update(identity)
    return case


def _cluster(cluster_id, case_ids, level="UVM_ERROR", error_id="E", location="dut.sv(1)"):
    return {
        "cluster_id": cluster_id,
        "signature": {"level": level, "error_id": error_id, "location": location, "description_template": "failure"},
        "case_ids": case_ids,
        "occurrence_count": len(case_ids),
    }


def test_case_identity_parses_trailing_numeric_seed():
    assert case_identity_from_case_id("suite/test_alpha_12.log") == {"test_id": "test_alpha", "seed": 12, "seed_parse_status": "parsed"}
    assert case_identity_from_case_id("suite/test_alpha.log") == {"test_id": "test_alpha", "seed": None, "seed_parse_status": "fallback"}


def test_recommendation_budget_prioritizes_fatal_and_seed_coverage():
    cases = [
        _case("test_a_2.log", level="UVM_FATAL", error_id="F", location="dut.sv(1)"),
        _case("test_b_1.log", error_id="E1", location="dut.sv(2)"),
        _case("test_b_2.log", error_id="E1", location="dut.sv(2)"),
        _case("test_a_1.log", error_id="E2", location="dut.sv(3)"),
    ]
    clusters = [
        _cluster("cluster-error-small", ["test_a_1.log"], error_id="E2", location="dut.sv(3)"),
        _cluster("cluster-fatal", ["test_a_2.log"], level="UVM_FATAL", error_id="F", location="dut.sv(1)"),
        _cluster("cluster-error-many-seeds", ["test_b_1.log", "test_b_2.log"], error_id="E1", location="dut.sv(2)"),
    ]

    recommendation = build_debug_recommendation(clusters, cases, 2)

    assert recommendation["debug_budget"] == 2
    assert recommendation["eligible_cluster_count"] == 3
    assert recommendation["selected_cluster_count"] == 2
    assert [item["cluster_id"] for item in recommendation["recommended_debug_cases"]] == ["cluster-fatal", "cluster-error-many-seeds"]
    assert recommendation["deferred_cluster_ids"] == ["cluster-error-small"]
    assert clusters[1]["recommendation"]["selected"] is True
    assert clusters[0]["recommendation"]["selected"] is False


def test_recommendation_prefers_shortest_time_within_cluster_over_test_diversity():
    cases = [
        _case("test_a_1.log", level="UVM_FATAL", error_id="F", location="dut.sv(1)", simulation_time=_simulation_time("1", "ns", "1000000", "max_observed_timestamp")),
        _case("test_a_2.log", error_id="E", location="dut.sv(2)", simulation_time=_simulation_time("10", "ns", "10000000", "explicit_end_marker")),
        _case("test_b_1.log", error_id="E", location="dut.sv(2)", simulation_time=_simulation_time("100", "ns", "100000000", "explicit_end_marker")),
    ]
    clusters = [
        _cluster("cluster-fatal", ["test_a_1.log"], level="UVM_FATAL", error_id="F", location="dut.sv(1)"),
        _cluster("cluster-error", ["test_a_2.log", "test_b_1.log"], error_id="E", location="dut.sv(2)"),
    ]

    recommendation = build_debug_recommendation(clusters, cases, 2)

    assert recommendation["policy_version"] == "deterministic.v2"
    assert recommendation["recommended_debug_cases"][1]["case_id"] == "test_a_2.log"
    assert recommendation["recommended_debug_cases"][1]["simulation_time"]["normalized_fs"] == "10000000"
    assert clusters[1]["recommendation"]["recommended_simulation_time"]["source"] == "explicit_end_marker"
    assert clusters[1]["recommendation"]["alternate_case_ids"] == ["test_b_1.log"]


def test_recommendation_places_known_time_before_unavailable_time():
    cases = [
        _case("test_a_1.log", simulation_time=_simulation_time()),
        _case("test_b_1.log", simulation_time=_simulation_time("5", "ns", "5000000", "max_observed_timestamp")),
    ]
    clusters = [_cluster("cluster-error", ["test_a_1.log", "test_b_1.log"])]

    recommendation = build_debug_recommendation(clusters, cases, 1)

    assert recommendation["recommended_debug_cases"][0]["case_id"] == "test_b_1.log"
