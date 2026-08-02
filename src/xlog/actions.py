import json
from pathlib import Path

from .bundle import scan_regression, write_bundle
from .config import DEFAULT_MAX_LOG_FILES, load_effective_parser_config
from .errors import XlogError
from .recommendation import DEFAULT_DEBUG_BUDGET


_ROOT = Path(__file__).resolve().parents[2]
_ACTIONS = {
    "actions": "List xlog public actions.",
    "schema": "Return a public JSON schema.",
    "scan": "Scan a regression directory and write xlog_bundle.v1.",
}
_ENVELOPE_FIELDS = {"api_version", "request_id", "action", "target", "args", "limits"}


def error_response(request_id, action, error):
    return {
        "api_version": "xlog.v1",
        "request_id": request_id,
        "action": action,
        "ok": False,
        "summary": {"status": "error", "error_code": error.code},
        "data": None,
        "error": {"code": error.code, "message": error.message, "details": error.details},
    }


def _success_response(request, summary, data):
    return {
        "api_version": "xlog.v1",
        "request_id": request.get("request_id"),
        "action": request["action"],
        "ok": True,
        "summary": summary,
        "data": data,
        "error": None,
    }


def _mapping(value, name):
    if not isinstance(value, dict):
        raise XlogError("INVALID_REQUEST", "%s must be an object" % name)
    return value


def _strict_fields(value, allowed, name):
    unknown = set(value) - set(allowed)
    if unknown:
        raise XlogError("INVALID_REQUEST", "%s contains unsupported fields" % name, {"fields": sorted(unknown)})


def _nonempty_string(value, name):
    if not isinstance(value, str) or not value.strip():
        raise XlogError("INVALID_REQUEST", "%s must be a non-empty string" % name)
    return value.strip()


def _validate_envelope(request):
    request = _mapping(request, "request")
    _strict_fields(request, _ENVELOPE_FIELDS, "request")
    if request.get("api_version") != "xlog.v1":
        raise XlogError("UNSUPPORTED_API_VERSION", "api_version must be xlog.v1")
    action = _nonempty_string(request.get("action"), "action")
    if action not in _ACTIONS:
        raise XlogError("UNKNOWN_ACTION", "unknown action", {"action": action, "available_values": sorted(_ACTIONS)})
    if "request_id" in request and request["request_id"] is not None and not isinstance(request["request_id"], str):
        raise XlogError("INVALID_REQUEST", "request_id must be a string or null")
    request["action"] = action
    return request


def _load_schema(kind):
    filenames = {"request": "xlog.v1.scan.request.schema.json", "bundle": "xlog_bundle.v1.schema.json"}
    if kind not in filenames:
        raise XlogError("INVALID_REQUEST", "unsupported schema kind", {"available_values": sorted(filenames)})
    try:
        with (_ROOT / "schemas" / filenames[kind]).open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        raise XlogError("INTERNAL_ERROR", "cannot load bundled schema", {"reason": str(exc)})


def _handle_schema(request):
    args = _mapping(request.get("args", {}), "args")
    _strict_fields(args, {"action", "kind"}, "args")
    action = _nonempty_string(args.get("action"), "args.action")
    if action != "scan":
        raise XlogError("INVALID_REQUEST", "schema is available only for scan", {"available_values": ["scan"]})
    kind = _nonempty_string(args.get("kind"), "args.kind")
    return _success_response(request, {"status": "ok"}, {"schema": _load_schema(kind)})


def _positive_int(value, name, default, maximum=None):
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise XlogError("INVALID_REQUEST", "%s must be a positive integer" % name)
    if maximum is not None and value > maximum:
        raise XlogError("INVALID_REQUEST", "%s exceeds its maximum" % name, {"maximum": maximum})
    return value


def _handle_scan(request):
    target = _mapping(request.get("target", {}), "target")
    _strict_fields(target, {"regression_root"}, "target")
    regression_root = _nonempty_string(target.get("regression_root"), "target.regression_root")

    args = _mapping(request.get("args", {}), "args")
    _strict_fields(args, {"output_path", "config_path", "parser"}, "args")
    output_path = _nonempty_string(args.get("output_path"), "args.output_path")
    config_path = args.get("config_path")
    if config_path is not None:
        config_path = _nonempty_string(config_path, "args.config_path")

    limits = _mapping(request.get("limits", {}), "limits")
    _strict_fields(limits, {"max_log_files", "workers", "debug_budget"}, "limits")
    max_log_files = _positive_int(limits.get("max_log_files"), "limits.max_log_files", DEFAULT_MAX_LOG_FILES)
    workers = _positive_int(limits.get("workers"), "limits.workers", None, maximum=64)
    debug_budget = _positive_int(limits.get("debug_budget"), "limits.debug_budget", DEFAULT_DEBUG_BUDGET)

    parser_config = load_effective_parser_config(config_path, args.get("parser"))
    bundle = scan_regression(regression_root, parser_config, max_log_files, workers, debug_budget)
    resolved_path, sha256 = write_bundle(bundle, output_path)
    return _success_response(
        request,
        dict(bundle["summary"]),
        {"bundle_api_version": bundle["api_version"], "bundle_path": resolved_path, "bundle_sha256": sha256},
    )


def dispatch_request(request):
    request = _validate_envelope(request)
    if request["action"] == "actions":
        return _success_response(request, {"status": "ok", "action_count": len(_ACTIONS)}, {"actions": _ACTIONS})
    if request["action"] == "schema":
        return _handle_schema(request)
    return _handle_scan(request)
