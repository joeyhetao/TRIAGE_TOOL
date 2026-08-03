import json

from xlog.cli import main


def test_actions_and_schema_shortcuts_emit_json(capsys):
    assert main(["actions"]) == 0
    actions = json.loads(capsys.readouterr().out)
    assert actions["ok"] is True
    assert sorted(actions["data"]["actions"]) == ["actions", "scan", "schema"]

    assert main(["schema", "--action", "scan", "--kind", "bundle"]) == 0
    schema = json.loads(capsys.readouterr().out)
    assert schema["data"]["schema"]["$id"] == "xlog_bundle.v1.schema.json"
    assert "debug_recommendation" in schema["data"]["schema"]["required"]

    assert main(["schema", "--action", "scan", "--kind", "request"]) == 0
    request_schema = json.loads(capsys.readouterr().out)
    assert request_schema["data"]["schema"]["required"] == ["api_version", "action", "target", "args"]
    assert "debug_budget" in request_schema["data"]["schema"]["properties"]["limits"]["properties"]
    assert "artifacts" in request_schema["data"]["schema"]["properties"]["args"]["properties"]
    assert "test_id" in schema["data"]["schema"]["definitions"]["case"]["properties"]


def test_scan_shortcut_and_json_error_are_machine_readable(tmp_path, capsys):
    root = tmp_path / "regression"
    root.mkdir()
    (root / "case.log").write_text("JVP TEST PASSED\n", encoding="utf-8")
    output = tmp_path / "bundle.json"
    assert main(["scan", "--root", str(root), "--output", str(output), "--debug-budget", "1"]) == 0
    response = json.loads(capsys.readouterr().out)
    assert response["data"]["bundle_path"] == str(output)
    bundle = json.loads(output.read_text(encoding="utf-8"))
    assert bundle["debug_recommendation"]["debug_budget"] == 1

    request = tmp_path / "bad.json"
    request.write_text("{not json", encoding="utf-8")
    assert main(["--json", str(request)]) == 2
    error = json.loads(capsys.readouterr().out)
    assert error["error"]["code"] == "INVALID_JSON"
