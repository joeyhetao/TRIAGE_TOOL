import json
from pathlib import Path

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


def _parser_mapping(value, source):
    if not isinstance(value, dict):
        raise XlogError("CONFIG_INVALID", "%s must be a JSON object" % source)
    unknown = set(value) - {"extra_patterns", "pass_patterns", "parser"}
    if unknown:
        raise XlogError("CONFIG_INVALID", "%s contains unsupported fields" % source, {"fields": sorted(unknown)})
    if "parser" in value:
        if len(value) != 1:
            raise XlogError("CONFIG_INVALID", "%s cannot mix parser with other fields" % source)
        return _parser_mapping(value["parser"], source + ".parser")
    parsed = {}
    if "extra_patterns" in value:
        parsed["extra_patterns"] = [item.upper() for item in _string_list(value["extra_patterns"], "extra_patterns")]
    if "pass_patterns" in value:
        parsed["pass_patterns"] = _string_list(value["pass_patterns"], "pass_patterns")
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
    return _parser_mapping(data, "config file")


def load_effective_parser_config(config_path=None, inline_parser=None):
    effective = {
        "extra_patterns": list(DEFAULT_EXTRA_PATTERNS),
        "pass_patterns": list(DEFAULT_PASS_PATTERNS),
    }
    if config_path:
        effective.update(_load_config_file(config_path))
    if inline_parser is not None:
        effective.update(_parser_mapping(inline_parser, "args.parser"))
    return effective
