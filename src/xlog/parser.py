import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .config import TOP_ERRORS_PER_CASE
from .dedup import description_signature


_UVM_PATTERN = re.compile(
    r"(?P<level>UVM_(?:ERROR|WARNING|FATAL))"
    r"(?:\([^)]*\))?"
    r"(?:\s+(?P<file>\S+)\((?P<line>\d+)\))?"
    r"\s+@\s*(?P<time>[\d.]+(?:\s*[a-z]+s)?)"
    r"\s*:\s*"
    r"(?:(?P<reporter>\S+)\s+)?"
    r"\[(?P<id>[^\]]*)\]\s*"
    r"(?P<msg>.*)",
    re.IGNORECASE,
)
_UVM_ANY = re.compile(r"UVM_(?:ERROR|WARNING|FATAL|INFO)\s", re.IGNORECASE)

_VCS_PATTERN = re.compile(r"^(?P<level>Error|Warning|Fatal|Note|Info)-\[(?P<id>[A-Z][A-Z0-9_-]*)\]\s*(?P<msg>.*)", re.IGNORECASE)
_VCS_ANY = re.compile(r"^(?:Error|Warning|Fatal|Note|Info)-\[", re.IGNORECASE)
_VCS_LEVEL_MAP = {"ERROR": "ERROR", "WARNING": "WARNING", "FATAL": "FATAL"}

_XCELIUM_PATTERN = re.compile(
    r"^(?P<tool>xrun|xmsim|xmelab|xmvlog|xmverilog|xmsd|ncsim|ncelab|ncvlog|irun)"
    r"(?:\(\d+\))?:\s*\*(?P<level>[A-Z]+),(?P<id>[A-Z][A-Z0-9_]*)"
    r"(?:\s*\((?P<file>[^,)]+),(?P<line>\d+)(?:\|\d+)?\))?:\s*(?P<msg>.*)",
    re.IGNORECASE,
)
_XCELIUM_ANY = re.compile(
    r"^(?:xrun|xmsim|xmelab|xmvlog|xmverilog|xmsd|ncsim|ncelab|ncvlog|irun)(?:\(\d+\))?:\s*\*",
    re.IGNORECASE,
)
_XCELIUM_LEVEL_MAP = {"E": "ERROR", "W": "WARNING", "F": "FATAL", "SE": "ERROR"}

_SVA_PATTERN = re.compile(r"^(?P<level>SVA_(?:ERROR|WARNING|FATAL))\s*:\s*(?P<msg>.*)", re.IGNORECASE)
_SVA_ANY = re.compile(r"^SVA_(?:ERROR|WARNING|FATAL)\s*:", re.IGNORECASE)
_SVA_LEVELS = ("SVA_ERROR", "SVA_WARNING", "SVA_FATAL")

_TIME_VALUE_PATTERN = r"(?P<time_value>(?:\d+(?:\.\d*)?|\.\d+))\s*(?P<time_unit>fs|ps|ns|us|ms|s)\b"
_AT_TIMESTAMP_PATTERN = re.compile(r"@\s*" + _TIME_VALUE_PATTERN, re.IGNORECASE)
_LABELED_TIME_PATTERN = re.compile(r"\b(?:simulation\s+)?time\s*[:=]\s*" + _TIME_VALUE_PATTERN, re.IGNORECASE)
_ANY_TIME_PATTERN = re.compile(_TIME_VALUE_PATTERN, re.IGNORECASE)
_SIMULATION_END_PATTERN = re.compile(
    r"\$(?:finish|stop)\b|\bend\s+of\s+simulation\b|\bsimulation\s+(?:complete(?:d)?|ended|finished|stopped)\b",
    re.IGNORECASE,
)
_CPU_OR_WALL_TIME_PATTERN = re.compile(r"\b(?:cpu|wall|elapsed|real)\s*(?:time|runtime)?\s*[:=]", re.IGNORECASE)
_TIME_TO_FS = {
    "fs": Decimal("1"),
    "ps": Decimal("1000"),
    "ns": Decimal("1000000"),
    "us": Decimal("1000000000"),
    "ms": Decimal("1000000000000"),
    "s": Decimal("1000000000000000"),
}
_VCS_REPORT_LOOKAHEAD_LINES = 20


def _build_gen_pattern(keywords):
    if not keywords:
        return None
    alternatives = "|".join(re.escape(keyword) for keyword in keywords)
    return re.compile(
        r"^(" + alternatives + r")\b[\s:\-]*(?:\[([^\]]+)\])?\s*(.*)",
        re.IGNORECASE,
    )


def _entry(level, timestamp, error_id, location, description, error_type=None):
    event_time = _time_from_text(timestamp, "reported_timestamp")
    return {
        "level": level,
        "timestamp": timestamp,
        "error_id": error_id,
        "report_id": error_id or None,
        "error_type": error_type or (level if str(level).upper().startswith("UVM_") else "UNKNOWN"),
        "location": location,
        "source_location": _source_location(location),
        "event_time": event_time,
        "description": description,
    }


def _source_location(location):
    text = str(location or "").strip()
    match = re.match(r"^(?P<path>.*)\((?P<line>\d+)\)$", text)
    if match:
        return {
            "path": os.path.normpath(match.group("path")).replace("\\", "/"),
            "line": int(match.group("line")),
            "display": text,
        }
    return {"path": None, "line": None, "display": text or None}


def _unavailable_event_time():
    return {
        "value": None,
        "unit": None,
        "normalized_fs": None,
        "source": "unavailable",
    }


def _time_from_text(value, source):
    match = _ANY_TIME_PATTERN.search(str(value or ""))
    return _simulation_time_from_match(match, source) if match else _unavailable_event_time()


def _unavailable_simulation_time():
    return {
        "value": None,
        "unit": None,
        "normalized_fs": None,
        "source": "unavailable",
    }


def _format_decimal(value):
    text = format(value.normalize(), "f")
    return "0" if text in ("", "-0") else text


def _simulation_time_from_match(match, source):
    raw_value = match.group("time_value")
    unit = match.group("time_unit").lower()
    try:
        normalized = Decimal(raw_value) * _TIME_TO_FS[unit]
    except (InvalidOperation, KeyError):
        return None
    return {
        "value": raw_value,
        "unit": unit,
        "normalized_fs": _format_decimal(normalized),
        "source": source,
    }


def _is_vcs_report_header(line):
    compact = re.sub(r"\s+", "", line).lower()
    return "vcssimulationreport" in compact


def _observed_simulation_times(line):
    if _CPU_OR_WALL_TIME_PATTERN.search(line):
        return []
    matches = list(_AT_TIMESTAMP_PATTERN.finditer(line))
    matches.extend(_LABELED_TIME_PATTERN.finditer(line))
    return [time for time in (_simulation_time_from_match(match, "max_observed_timestamp") for match in matches) if time]


def _explicit_end_simulation_time(line, in_vcs_report):
    if _CPU_OR_WALL_TIME_PATTERN.search(line):
        return None
    if _SIMULATION_END_PATTERN.search(line):
        match = _ANY_TIME_PATTERN.search(line)
        return _simulation_time_from_match(match, "explicit_end_marker") if match else None
    if in_vcs_report:
        match = _LABELED_TIME_PATTERN.search(line)
        return _simulation_time_from_match(match, "explicit_end_marker") if match else None
    return None


def _time_is_larger(candidate, current):
    return current is None or Decimal(candidate["normalized_fs"]) > Decimal(current["normalized_fs"])


def _xcelium_error_type(tool):
    name = str(tool or "").lower()
    if name in ("xmvlog", "xmverilog", "ncvlog"):
        return "COMPILE_ERROR"
    if name in ("xmelab", "ncelab"):
        return "ELABORATION_ERROR"
    return "SIM_RUNTIME_ERROR"


def _error_result(filepath, error_msg):
    return {
        "file": Path(filepath).name,
        "filepath": str(filepath),
        "statistics": {"UVM_WARNING": 0, "UVM_ERROR": 0, "UVM_FATAL": 0},
        "status": "error",
        "pass_found": False,
        "top_errors": [],
        "all_errors": [],
        "primary_error": None,
        "simulation_time": _unavailable_simulation_time(),
        "parse_error": {"code": "LOG_READ_FAILED", "message": error_msg},
    }


def parse_log(filepath, extra_keywords=None, pass_patterns=None):
    """Parse one log with bounded memory and retain the first five errors."""
    path = Path(filepath)
    extra_keywords = [keyword.upper() for keyword in (extra_keywords or [])]
    pass_patterns = list(pass_patterns or [])
    statistics = {"UVM_WARNING": 0, "UVM_ERROR": 0, "UVM_FATAL": 0}
    for level in _SVA_LEVELS:
        statistics.setdefault(level, 0)
    for keyword in extra_keywords:
        statistics.setdefault(keyword, 0)

    general_pattern = _build_gen_pattern(extra_keywords)
    pass_found = False
    top_errors = []
    pending = None
    continuation_lines = []
    explicit_simulation_time = None
    max_observed_simulation_time = None
    vcs_report_lines_remaining = 0

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.rstrip("\n")
            stripped = line.strip()

            is_vcs_report_header = _is_vcs_report_header(line)
            in_vcs_report = is_vcs_report_header or vcs_report_lines_remaining > 0
            if is_vcs_report_header:
                vcs_report_lines_remaining = _VCS_REPORT_LOOKAHEAD_LINES
            elif vcs_report_lines_remaining > 0:
                vcs_report_lines_remaining -= 1

            explicit_time = _explicit_end_simulation_time(line, in_vcs_report)
            if explicit_time is not None:
                explicit_simulation_time = explicit_time
            for observed_time in _observed_simulation_times(line):
                if _time_is_larger(observed_time, max_observed_simulation_time):
                    max_observed_simulation_time = observed_time

            if not pass_found and pass_patterns and any(pattern in line for pattern in pass_patterns):
                pass_found = True

            if pending is not None:
                is_continuation = (
                    stripped
                    and not _UVM_ANY.search(stripped)
                    and not _VCS_ANY.match(stripped)
                    and not _XCELIUM_ANY.match(stripped)
                    and not _SVA_ANY.match(stripped)
                    and line.startswith(" ")
                    and len(continuation_lines) < 3
                )
                if is_continuation:
                    continuation_lines.append(stripped)
                    continue
                if continuation_lines:
                    pending["description"] = (pending["description"] + " " + " ".join(continuation_lines)).strip()
                top_errors.append(pending)
                pending = None
                continuation_lines = []

            match = _UVM_PATTERN.search(line)
            if match:
                level = match.group("level").upper()
                statistics[level] = statistics.get(level, 0) + 1
                if level != "UVM_WARNING" and len(top_errors) < TOP_ERRORS_PER_CASE:
                    source_file = match.group("file")
                    source_line = match.group("line")
                    location = "%s(%s)" % (source_file, source_line) if source_file else ""
                    pending = _entry(
                        level,
                        match.group("time").replace(" ", ""),
                        match.group("id").strip(),
                        location,
                        match.group("msg").strip(),
                    )
                    continuation_lines = []
                continue

            vcs_match = _VCS_PATTERN.match(stripped)
            if vcs_match:
                level = _VCS_LEVEL_MAP.get(vcs_match.group("level").upper())
                if level:
                    statistics[level] = statistics.get(level, 0) + 1
                    if level != "WARNING" and len(top_errors) < TOP_ERRORS_PER_CASE:
                        pending = _entry(level, "", vcs_match.group("id").strip(), "", vcs_match.group("msg").strip(), "UNKNOWN")
                        continuation_lines = []
                continue

            xcelium_match = _XCELIUM_PATTERN.match(stripped)
            if xcelium_match:
                level = _XCELIUM_LEVEL_MAP.get(xcelium_match.group("level").upper())
                if level:
                    statistics[level] = statistics.get(level, 0) + 1
                    if level != "WARNING" and len(top_errors) < TOP_ERRORS_PER_CASE:
                        source_file = xcelium_match.group("file")
                        source_line = xcelium_match.group("line")
                        location = "%s(%s)" % (source_file, source_line) if source_file else ""
                        pending = _entry(level, "", xcelium_match.group("id").strip(), location, xcelium_match.group("msg").strip(), _xcelium_error_type(xcelium_match.group("tool")))
                        continuation_lines = []
                continue

            sva_match = _SVA_PATTERN.match(stripped)
            if sva_match:
                level = sva_match.group("level").upper()
                statistics[level] = statistics.get(level, 0) + 1
                if level != "SVA_WARNING" and len(top_errors) < TOP_ERRORS_PER_CASE:
                    pending = _entry(level, "", "", "", sva_match.group("msg").strip(), "SV_ASSERTION")
                    continuation_lines = []
                continue

            if general_pattern:
                general_match = general_pattern.match(stripped)
                if general_match:
                    level = general_match.group(1).upper()
                    statistics[level] = statistics.get(level, 0) + 1
                    if "WARNING" not in level and len(top_errors) < TOP_ERRORS_PER_CASE:
                        pending = _entry(level, "", (general_match.group(2) or "").strip(), "", general_match.group(3).strip(), "UNKNOWN")
                        continuation_lines = []

    if pending is not None:
        if continuation_lines:
            pending["description"] = (pending["description"] + " " + " ".join(continuation_lines)).strip()
        top_errors.append(pending)

    for error in top_errors:
        error["description_template"] = description_signature(error.get("description", ""))
        error["description_template_status"] = "present"

    primary_error = top_errors[0] if top_errors else None
    non_warning = {key: value for key, value in statistics.items() if "WARNING" not in key.upper()}
    has_error = any(non_warning.values())
    status = "pass" if ((not has_error and pass_found) if pass_patterns else not has_error) else "fail"
    return {
        "file": path.name,
        "filepath": str(path),
        "statistics": statistics,
        "status": status,
        "pass_found": pass_found,
        "top_errors": top_errors,
        "all_errors": [primary_error] if primary_error else [],
        "primary_error": primary_error,
        "simulation_time": explicit_simulation_time or max_observed_simulation_time or _unavailable_simulation_time(),
    }


def parse_logs(filepaths, progress_cb=None, extra_keywords=None, pass_patterns=None, workers=None):
    total = len(filepaths)
    if total == 0:
        return []
    if total == 1:
        try:
            result = parse_log(filepaths[0], extra_keywords, pass_patterns)
        except Exception as exc:
            result = _error_result(filepaths[0], str(exc))
        if progress_cb:
            progress_cb(Path(filepaths[0]).name, result, 1, 1)
        return [result]

    results = {}
    max_workers = workers or min(32, (os.cpu_count() or 1) + 4)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_index = {
            executor.submit(parse_log, filepath, extra_keywords, pass_patterns): (index, filepath)
            for index, filepath in enumerate(filepaths)
        }
        done = 0
        for future in as_completed(future_to_index):
            index, filepath = future_to_index[future]
            try:
                result = future.result()
            except Exception as exc:
                result = _error_result(filepath, str(exc))
            results[index] = result
            done += 1
            if progress_cb:
                progress_cb(Path(filepath).name, result, done, total)
    return [results[index] for index in range(total)]
