# -*- coding: utf-8 -*-
import re
from pathlib import Path


# 标准UVM报错正则
_UVM_PATTERN = re.compile(
    r'(UVM_(?:ERROR|WARNING|FATAL))'   # Group1: 级别
    r'\s+@\s+([\d.]+\w+)'              # Group2: 时间戳
    r'\s+\[(\w+)\]'                    # Group3: 错误ID
    r'\s+(\S+):\s*(.*)',               # Group4: 文件位置, Group5: 描述
    re.IGNORECASE
)

LEVELS = ['UVM_FATAL', 'UVM_ERROR', 'UVM_WARNING']
LEVEL_PRIORITY = {lvl: i for i, lvl in enumerate(LEVELS)}


def parse_log(filepath: str) -> dict:
    """
    解析单个仿真日志文件，返回结构化结果。
    """
    path = Path(filepath)
    lines = path.read_text(encoding='utf-8', errors='replace').splitlines()

    statistics = {'UVM_WARNING': 0, 'UVM_ERROR': 0, 'UVM_FATAL': 0}
    all_errors = []
    first_error = None

    for i, line in enumerate(lines):
        m = _UVM_PATTERN.search(line)
        if not m:
            continue

        level       = m.group(1).upper()
        timestamp   = m.group(2)
        error_id    = m.group(3)
        location    = m.group(4)
        description = m.group(5).strip()

        # 尝试读取下一行作为补充描述
        if i + 1 < len(lines):
            next_line = lines[i + 1].strip()
            if next_line and not _UVM_PATTERN.search(next_line):
                description = (description + ' ' + next_line).strip()

        if level in statistics:
            statistics[level] += 1

        entry = {
            'level':       level,
            'timestamp':   timestamp,
            'error_id':    error_id,
            'location':    location,
            'description': description,
        }
        all_errors.append(entry)

        # 首错：取优先级最高的首次出现（FATAL > ERROR > WARNING）
        if first_error is None or (
            LEVEL_PRIORITY.get(level, 99) <
            LEVEL_PRIORITY.get(first_error['level'], 99)
        ):
            first_error = entry

    return {
        'file':        path.name,
        'filepath':    str(filepath),
        'statistics':  statistics,
        'first_error': first_error,
        'all_errors':  all_errors,
    }


def parse_logs(filepaths: list) -> list:
    """解析多个日志文件，返回结果列表。"""
    return [parse_log(fp) for fp in filepaths]
