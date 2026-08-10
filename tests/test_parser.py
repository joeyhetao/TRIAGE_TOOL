from xlog.parser import parse_log, parse_logs


def _write_log(tmp_path, name, content):
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return str(path)


def test_parser_keeps_first_five_non_warning_errors(tmp_path):
    content = "\n".join(
        "UVM_ERROR /tb/dut.sv(%d) @ %dns: reporter [ERR_%d] failure %d" % (index, index, index, index)
        for index in range(7)
    )
    result = parse_log(_write_log(tmp_path, "many.log", content))
    assert result["status"] == "fail"
    assert len(result["top_errors"]) == 5
    assert result["primary_error"]["error_id"] == "ERR_0"
    assert len(result["all_errors"]) == 1


def test_parser_merges_continuation_and_excludes_warning(tmp_path):
    result = parse_log(_write_log(
        tmp_path,
        "continuation.log",
        "UVM_WARNING /tb/dut.sv(1) @ 1ns: reporter [WARN] ignore\n"
        "UVM_ERROR /tb/dut.sv(2) @ 2ns: reporter [ERR] first line\n"
        "  expected=0x1 actual=0x2\n",
    ))
    assert result["statistics"]["UVM_WARNING"] == 1
    assert len(result["top_errors"]) == 1
    assert result["top_errors"][0]["description"] == "first line expected=0x1 actual=0x2"
    assert result["top_errors"][0]["event_time"]["normalized_fs"] == "2000000"
    assert result["top_errors"][0]["source_location"] == {"path": "/tb/dut.sv", "line": 2, "display": "/tb/dut.sv(2)"}


def test_parser_supports_vcs_xcelium_sva_and_custom_patterns(tmp_path):
    result = parse_log(
        _write_log(
            tmp_path,
            "formats.log",
            "Error-[CNST-CIF] constraint failure\n"
            "xrun: *E,DSEM2009 (dut.sv,17): xcelium failure\n"
            "SVA_FATAL: assertion failure\n"
            "PROJECT_FATAL [MY_ID] project failure\n",
        ),
        extra_keywords=["PROJECT_FATAL"],
        pass_patterns=[],
    )
    assert [error["error_id"] for error in result["top_errors"]] == ["CNST-CIF", "DSEM2009", "", "MY_ID"]
    assert result["top_errors"][1]["location"] == "dut.sv(17)"
    assert result["top_errors"][1]["error_type"] == "SIM_RUNTIME_ERROR"
    assert result["top_errors"][2]["error_type"] == "SV_ASSERTION"
    assert result["top_errors"][0]["producer"] == "vcs"
    assert result["top_errors"][0]["scope_hint"] == {
        "candidate": "shared_public",
        "status": "non_authoritative",
        "producer": "vcs",
        "basis": ["producer=vcs", "error_type=TOOL_ERROR"],
        "final_routing": "undetermined",
    }
    assert result["top_errors"][3]["scope_hint"]["candidate"] == "environment_private"
    assert result["top_errors"][0]["portable_signature"]["fingerprint"].startswith("sha256:")
    assert result["status"] == "fail"


def test_public_description_template_removes_path_line_and_random_values(tmp_path):
    result = parse_log(_write_log(
        tmp_path,
        "public.log",
        "Error-[SE] /project/a/rtl/dut.sv:17 failed near token 918273\n",
    ))
    error = result["primary_error"]
    assert error["description"] == "/project/a/rtl/dut.sv:17 failed near token 918273"
    assert error["description_template"] == "<path> failed near token <num>"
    assert error["scope_hint"]["final_routing"] == "undetermined"


def test_parser_prefers_explicit_simulation_end_time(tmp_path):
    result = parse_log(_write_log(
        tmp_path,
        "explicit-end.log",
        "UVM_ERROR /tb/dut.sv(1) @ 50ns: reporter [ERR] failure\n"
        "$finish called from /tb/top.sv at simulation time 100ns\n"
        "UVM_INFO @ 250ns: reporter [RPT] later message\n",
    ))
    assert result["simulation_time"] == {
        "value": "100",
        "unit": "ns",
        "normalized_fs": "100000000",
        "source": "explicit_end_marker",
    }


def test_parser_reads_time_from_vcs_simulation_report(tmp_path):
    result = parse_log(_write_log(
        tmp_path,
        "vcs-report.log",
        "UVM_ERROR /tb/dut.sv(1) @ 50ns: reporter [ERR] failure\n"
        "V C S   S i m u l a t i o n   R e p o r t\n"
        "Time: 2us\n"
        "CPU Time: 99s\n",
    ))
    assert result["simulation_time"] == {
        "value": "2",
        "unit": "us",
        "normalized_fs": "2000000000",
        "source": "explicit_end_marker",
    }


def test_parser_falls_back_to_largest_simulation_timestamp_and_ignores_cpu_time(tmp_path):
    result = parse_log(_write_log(
        tmp_path,
        "fallback.log",
        "UVM_ERROR /tb/dut.sv(1) @ 900ns: reporter [ERR] failure\n"
        "UVM_INFO @ 1us: reporter [RPT] final timestamp\n"
        "CPU Time: 99s\n",
    ))
    assert result["simulation_time"] == {
        "value": "1",
        "unit": "us",
        "normalized_fs": "1000000000",
        "source": "max_observed_timestamp",
    }


def test_parser_reports_unavailable_simulation_time(tmp_path):
    result = parse_log(_write_log(tmp_path, "no-time.log", "JVP TEST PASSED\n"), pass_patterns=["JVP TEST PASSED"])
    assert result["simulation_time"] == {
        "value": None,
        "unit": None,
        "normalized_fs": None,
        "source": "unavailable",
    }


def test_parser_pass_semantics_follow_configured_marker(tmp_path):
    log_path = _write_log(tmp_path, "pass.log", "UVM_WARNING /tb/dut.sv(1) @ 1ns: reporter [WARN] benign\nJVP TEST PASSED\n")
    assert parse_log(log_path, pass_patterns=["JVP TEST PASSED"])["status"] == "pass"
    assert parse_log(log_path, pass_patterns=[])["status"] == "pass"


def test_parser_accepts_zero_error_uvm_report_summary_as_pass_evidence(tmp_path):
    log_path = _write_log(
        tmp_path,
        "uvm-summary-pass.log",
        "UVM_INFO @ 1885000ps: reporter [3P_CACHE_MAIN_PASS] completed\n"
        "--- UVM Report Summary ---\n"
        "UVM_INFO : 12\n"
        "UVM_WARNING : 0\n"
        "UVM_ERROR : 0\n"
        "UVM_FATAL : 0\n",
    )

    result = parse_log(log_path, pass_patterns=["JVP TEST PASSED"])

    assert result["status"] == "pass"
    assert result["pass_found"] is True


def test_parser_rejects_nonzero_uvm_report_summary_without_pass_marker(tmp_path):
    log_path = _write_log(
        tmp_path,
        "uvm-summary-fail.log",
        "--- UVM Report Summary ---\n"
        "UVM_ERROR : 1\n"
        "UVM_FATAL : 0\n",
    )

    result = parse_log(log_path, pass_patterns=["JVP TEST PASSED"])

    assert result["status"] == "fail"
    assert result["pass_found"] is False


def test_batch_parser_isolates_unreadable_log(tmp_path):
    good_path = _write_log(tmp_path, "good.log", "JVP TEST PASSED\n")
    results = parse_logs([good_path, str(tmp_path / "missing.log")], pass_patterns=["JVP TEST PASSED"], workers=1)
    assert results[0]["status"] == "pass"
    assert results[1]["status"] == "error"
    assert results[1]["simulation_time"]["source"] == "unavailable"
    assert results[1]["parse_error"]["code"] == "LOG_READ_FAILED"
