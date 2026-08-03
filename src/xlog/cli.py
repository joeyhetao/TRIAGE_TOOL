import argparse
import json
import sys

from .actions import dispatch_request, error_response
from .errors import XlogError


def _write_json(value):
    sys.stdout.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")


def _read_request(path):
    try:
        if path == "-":
            return json.load(sys.stdin)
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        raise XlogError("INVALID_JSON", "cannot read JSON request", {"reason": str(exc), "path": path})


def _build_parser():
    parser = argparse.ArgumentParser(prog="xlog", description="Regression log scanner")
    parser.add_argument("--json", metavar="REQUEST", help="Read one xlog.v1 JSON request from a file or stdin (-).")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("actions", help="List public actions.")
    schema = commands.add_parser("schema", help="Print a public schema response.")
    schema.add_argument("--action", required=True, choices=["scan"])
    schema.add_argument("--kind", required=True, choices=["request", "bundle"])
    scan = commands.add_parser("scan", help="Scan a regression directory.")
    scan.add_argument("--root", required=True, help="Absolute regression root directory.")
    scan.add_argument("--output", required=True, help="Absolute xlog_bundle.v1 JSON path.")
    scan.add_argument("--config", help="Optional absolute scan JSON configuration path for parser and artifact rules.")
    scan.add_argument("--max-log-files", type=int)
    scan.add_argument("--workers", type=int)
    scan.add_argument("--debug-budget", type=int, help="Maximum failure clusters recommended for downstream xdebug. Default: 20.")
    return parser


def _shortcut_request(args):
    if args.command == "actions":
        return {"api_version": "xlog.v1", "action": "actions"}
    if args.command == "schema":
        return {"api_version": "xlog.v1", "action": "schema", "args": {"action": args.action, "kind": args.kind}}
    if args.command == "scan":
        request = {
            "api_version": "xlog.v1",
            "action": "scan",
            "target": {"regression_root": args.root},
            "args": {"output_path": args.output},
        }
        if args.config:
            request["args"]["config_path"] = args.config
        limits = {}
        if args.max_log_files is not None:
            limits["max_log_files"] = args.max_log_files
        if args.workers is not None:
            limits["workers"] = args.workers
        if args.debug_budget is not None:
            limits["debug_budget"] = args.debug_budget
        if limits:
            request["limits"] = limits
        return request
    return None


def main(argv=None):
    args = _build_parser().parse_args(argv)
    request_id = None
    action = None
    try:
        request = _read_request(args.json) if args.json else _shortcut_request(args)
        if request is None:
            raise XlogError("INVALID_REQUEST", "choose an action or --json request")
        if isinstance(request, dict):
            request_id = request.get("request_id")
            action = request.get("action")
        _write_json(dispatch_request(request))
        return 0
    except XlogError as exc:
        _write_json(error_response(request_id, action, exc))
        return 2
    except Exception as exc:
        _write_json(error_response(request_id, action, XlogError("INTERNAL_ERROR", "unexpected xlog failure", {"reason": str(exc)})))
        return 1
