from xlog.dedup import build_failure_clusters, description_signature


def _case(case_id, error):
    return {"case_id": case_id, "log_path": "/regress/" + case_id, "status": "fail", "primary_error": error}


def _error(description, error_id="rpe_qp", location="rpe_qp.sv(3495)"):
    return {"level": "UVM_ERROR", "error_id": error_id, "location": location, "description": description}


def test_id_and_location_merge_dynamic_runtime_dumps():
    cases = [
        _case("b.log", _error("END_OF_QP_CHECK HCA_LCL_QP<pipe0><FUNC'h1e> qpc.sq_tx_pi('h9)!=qpc.sq_tx_cur_ci('h0)")),
        _case("a.log", _error("END_OF_QP_CHECK HCA_LCL_QP<pipe1><FUNC'h789> qpc.sq_tx_pi('ha)!=qpc.sq_tx_cur_ci('h1)")),
    ]
    clusters, unclustered = build_failure_clusters(cases)
    assert unclustered == []
    assert len(clusters) == 1
    assert clusters[0]["case_ids"] == ["a.log", "b.log"]
    assert clusters[0]["representative_case_id"] == "a.log"
    assert clusters[0]["signature"]["strategy"] == "level_error_id_location"


def test_location_or_error_id_changes_split_clusters():
    cases = [
        _case("a.log", _error("message", location="a.sv(1)")),
        _case("b.log", _error("message", location="b.sv(1)")),
        _case("c.log", _error("message", error_id="other", location="a.sv(1)")),
    ]
    clusters, _ = build_failure_clusters(cases)
    assert len(clusters) == 3


def test_fallback_signature_normalizes_sv_hex_and_decimal_values():
    assert description_signature("function=187") == description_signature("function=7fc")
    assert description_signature("pkt_len('d70) value=4294967288") == description_signature("pkt_len(32'hff) value=12")
    assert "<num>fc" not in description_signature("function=7fc")
    assert description_signature("10th retry") == "10th retry"


def test_missing_primary_error_is_reported_as_unclustered_failure():
    clusters, unclustered = build_failure_clusters([{"case_id": "no_marker.log", "log_path": "/r/no_marker.log", "status": "fail", "primary_error": None}])
    assert clusters == []
    assert unclustered == ["no_marker.log"]
