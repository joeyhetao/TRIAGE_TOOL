import json
import string
from pathlib import Path

from .artifacts import DEFAULT_ARTIFACT_RULES
from .errors import XlogError


DEFAULT_EXTRA_PATTERNS = [
    "ERROR",
    "FATAL",
    "FAILED",
    "VIRL_MEM_WARNING",
    "JVP TEST FAILED",
    "SVA_ERROR",
    "SVA_FATAL",
    "SVA_WARNING",
]
DEFAULT_PASS_PATTERNS = ["JVP TEST PASSED"]
DEFAULT_MAX_LOG_FILES = 5000
TOP_ERRORS_PER_CASE = 5
_TEMPLATE_FIELDS = {"log_dir", "log_stem", "test_id", "regression_root"}


def _string_list(value, field):
    if not isinstance(value, list):
        raise XlogError("CONFIG_INVALID", "%s must be an array" % field)
    result = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise XlogError("CONFIG_INVALID", "%s entries must be non-empty strings" % field)
        normalized = item.strip()
        if normalized not in result:
            result.append(normalized)
    return result


def _validate_template(template, field):
    try:
        fields = [name for _, name, _, _ in string.Formatter().parse(template) if name]
    except ValueError as exc:
        raise XlogError("CONFIG_INVALID", "%s contains an invalid template" % field, {"reason": str(exc)})
    unknown = set(fields) - _TEMPLATE_FIELDS
    if unknown:
        raise XlogError("CONFIG_INVALID", "%s contains unsupported placeholders" % field, {"fields": sorted(unknown)})


def _artifact_mapping(value, source):
    if not isinstance(value, dict):
        raise XlogError("CONFIG_INVALID", "%s must be a JSON object" % source)
    allowed = {
        "log_reference_extraction",
        "fsdb_templates",
        "daidir_templates",
        "kdb_templates",
        "run_manifest_templates",
        "xdebug_run_manifest_templates",
    }
    unknown = set(value) - allowed
    if unknown:
        raise XlogError("CONFIG_INVALID", "%s contains unsupported fields" % source, {"fields": sorted(unknown)})
    parsed = {}
    if "log_reference_extraction" in value:
        if not isinstance(value["log_reference_extraction"], bool):
            raise XlogError("CONFIG_INVALID", "%s.log_reference_extraction must be a boolean" % source)
        parsed["log_reference_extraction"] = value["log_reference_extraction"]
    for field in sorted(allowed - {"log_reference_extraction"}):
        if field not in value:
            continue
        templates = _string_list(value[field], "%s.%s" % (source, field))
        for template in templates:
            _validate_template(template, "%s.%s" % (source, field))
        parsed[field] = templates
    return parsed


def _parser_mapping(value, source):
    if not isinstance(value, dict):
        raise XlogError("CONFIG_INVALID", "%s must be a JSON object" % source)
    unknown = set(value) - {"extra_patterns", "pass_patterns"}
    if unknown:
        raise XlogError("CONFIG_INVALID", "%s contains unsupported fields" % source, {"fields": sorted(unknown)})
    parsed = {}
    if "extra_patterns" in value:
        parsed["extra_patterns"] = [item.upper() for item in _string_list(value["extra_patterns"], "extra_patterns")]
    if "pass_patterns" in value:
        parsed["pass_patterns"] = _string_list(value["pass_patterns"], "pass_patterns")
    return parsed


def _scan_mapping(value, source):
    if not isinstance(value, dict):
        raise XlogError("CONFIG_INVALID", "%s must be a JSON object" % source)
    unknown = set(value) - {"extra_patterns", "pass_patterns", "parser", "artifacts"}
    if unknown:
        raise XlogError("CONFIG_INVALID", "%s contains unsupported fields" % source, {"fields": sorted(unknown)})
    parser_fields = {key: value[key] for key in ("extra_patterns", "pass_patterns") if key in value}
    if "parser" in value and parser_fields:
        raise XlogError("CONFIG_INVALID", "%s cannot mix parser with parser fields" % source)
    parsed = {"parser": _parser_mapping(value.get("parser", parser_fields), source + ".parser")}
    if "artifacts" in value:
        parsed["artifacts"] = _artifact_mapping(value["artifacts"], source + ".artifacts")
    return parsed


def _load_config_file(config_path):
    path = Path(config_path).expanduser()
    if not path.is_absolute():
        raise XlogError("CONFIG_INVALID", "config_path must be absolute")
    if not path.is_file():
        raise XlogError("CONFIG_NOT_FOUND", "config file does not exist", {"config_path": str(path)})
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise XlogError("CONFIG_INVALID", "cannot read config file", {"reason": str(exc)})
    return _scan_mapping(data, "config file")


def load_effective_scan_config(config_path=None, inline_parser=None, inline_artifacts=None):
    parser = {
        "extra_patterns": list(DEFAULT_EXTRA_PATTERNS),
        "pass_patterns": list(DEFAULT_PASS_PATTERNS),
    }
    artifacts = dict((key, list(value) if isinstance(value, list) else value) for key, value in DEFAULT_ARTIFACT_RULES.items())
    if config_path:
        configured = _load_config_file(config_path)
        parser.update(configured["parser"])
        artifacts.update(configured.get("artifacts", {}))
    if inline_parser is not None:
        parser.update(_parser_mapping(inline_parser, "args.parser"))
    if inline_artifacts is not None:
        artifacts.update(_artifact_mapping(inline_artifacts, "args.artifacts"))
    return {"parser": parser, "artifacts": artifacts}


def load_effective_parser_config(config_path=None, inline_parser=None):
    """Compatibility wrapper for parser-only callers."""
    return load_effective_scan_config(config_path, inline_parser)["parser"]
