from pathlib import Path

from .errors import XlogError


def _path_key(path, root):
    return path.relative_to(root).as_posix()


def _select_directory_logs(paths):
    candidates = sorted(paths, key=lambda path: path.name.casefold())
    if len(candidates) == 1:
        return candidates

    directory_name = candidates[0].parent.name.casefold()
    matching = [
        path
        for path in candidates
        if directory_name == path.stem.casefold()
        or directory_name.endswith(path.stem.casefold())
    ]
    return matching if len(matching) == 1 else candidates


def discover_log_files(regression_root, max_log_files):
    root = Path(regression_root).expanduser()
    if not root.is_absolute():
        raise XlogError("INVALID_REQUEST", "regression_root must be absolute")
    if not root.is_dir():
        raise XlogError("REGRESSION_ROOT_NOT_FOUND", "regression_root is not a directory", {"regression_root": str(root)})
    try:
        resolved_root = root.resolve()
        grouped = {}
        for path in resolved_root.rglob("*"):
            if (
                path.suffix.lower() != ".log"
                or path.name.lower().endswith("_bk.log")
                or not path.is_file()
            ):
                continue
            grouped.setdefault(path.parent, []).append(path)

        selected = (
            path
            for parent in sorted(grouped, key=lambda path: _path_key(path, resolved_root))
            for path in _select_directory_logs(grouped[parent])
        )
        files = sorted(
            (path.resolve() for path in selected),
            key=lambda path: _path_key(path, resolved_root),
        )
    except OSError as exc:
        raise XlogError("SCAN_FAILED", "cannot scan regression_root", {"reason": str(exc)})
    if not files:
        raise XlogError("NO_LOG_FILES", "no .log files found", {"regression_root": str(resolved_root)})
    if len(files) > max_log_files:
        raise XlogError("LOG_FILE_LIMIT_EXCEEDED", "discovered log count exceeds max_log_files", {"discovered": len(files), "max_log_files": max_log_files})
    return resolved_root, files
