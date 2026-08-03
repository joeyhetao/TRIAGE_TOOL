from pathlib import Path

from .errors import XlogError


def discover_log_files(regression_root, max_log_files):
    root = Path(regression_root).expanduser()
    if not root.is_absolute():
        raise XlogError("INVALID_REQUEST", "regression_root must be absolute")
    if not root.is_dir():
        raise XlogError("REGRESSION_ROOT_NOT_FOUND", "regression_root is not a directory", {"regression_root": str(root)})
    try:
        resolved_root = root.resolve()
        files = sorted(
            (
                path.resolve()
                for path in resolved_root.rglob("*")
                if path.is_file()
                and path.suffix.lower() == ".log"
                and not path.name.lower().endswith("_bk.log")
            ),
            key=lambda path: path.relative_to(resolved_root).as_posix(),
        )
    except OSError as exc:
        raise XlogError("SCAN_FAILED", "cannot scan regression_root", {"reason": str(exc)})
    if not files:
        raise XlogError("NO_LOG_FILES", "no .log files found", {"regression_root": str(resolved_root)})
    if len(files) > max_log_files:
        raise XlogError("LOG_FILE_LIMIT_EXCEEDED", "discovered log count exceeds max_log_files", {"discovered": len(files), "max_log_files": max_log_files})
    return resolved_root, files
