# -*- coding: utf-8 -*-
import re
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor


# 标准VCS/UVM报错正则
# 格式: UVM_ERROR /path/file.sv(142) @ 1000ns: uvm_test_top.env [ID] message
_UVM_PATTERN = re.compile(
    r'(UVM_(?:ERROR|WARNING|FATAL))'   # Group1: 级别
    r'\s+(\S+)\((\d+)\)'              # Group2: 文件路径, Group3: 行号
    r'\s+@\s+([\d.]+\s*\w+)'          # Group4: 时间戳
    r':\s+(\S+)\s+'                   # Group5: 组件路径
    r'\[(\w+)\]\s*(.*)',              # Group6: 错误ID, Group7: 描述
    re.IGNORECASE
)

# 匹配任意UVM行（含INFO），用于续行检测时排除
_UVM_ANY = re.compile(r'UVM_(?:ERROR|WARNING|FATAL|INFO)\s', re.IGNORECASE)

TOP_N = 5  # 每个日志最多提取的错误条数（按出现顺序）


def parse_log(filepath: str) -> dict:
    """
    逐行流式解析单个仿真日志文件，返回结构化结果。
    内存占用与文件大小无关，仅保留当前处理窗口（续行缓冲最多3行）。
    top_errors: 按出现顺序提取前 TOP_N 条错误，每条尝试合并后续续行描述。
    """
    path = Path(filepath)
    statistics = {'UVM_WARNING': 0, 'UVM_ERROR': 0, 'UVM_FATAL': 0}
    top_errors = []
    pending = None    # 等待续行收集的当前条目（仅 top_errors 未满时使用）
    cont_lines = []   # 已收集的续行文本

    with open(str(path), encoding='utf-8', errors='replace') as f:
        for raw_line in f:
            line = raw_line.rstrip('\n')
            stripped = line.strip()

            # ── 续行收集 ──────────────────────────────────────────
            if pending is not None:
                if (stripped
                        and not _UVM_ANY.search(stripped)
                        and line.startswith(' ')
                        and len(cont_lines) < 3):
                    cont_lines.append(stripped)
                    continue
                # 续行终止（遇到空行 / UVM条目 / 非缩进行 / 已满3行）：提交 pending
                if cont_lines:
                    pending['description'] = (
                        pending['description'] + ' ' + ' '.join(cont_lines)
                    ).strip()
                top_errors.append(pending)
                pending = None
                cont_lines = []
                # 当前行继续向下走，检查是否为新的 UVM 条目

            # ── UVM 条目匹配 ──────────────────────────────────────
            m = _UVM_PATTERN.search(line)
            if not m:
                continue

            level = m.group(1).upper()
            if level in statistics:
                statistics[level] += 1

            # top_errors 未满时才记录条目（仍需对全文统计错误数）
            if len(top_errors) < TOP_N:
                pending = {
                    'level':       level,
                    'timestamp':   m.group(4).replace(' ', ''),
                    'error_id':    m.group(6),
                    'location':    f"{m.group(2)}({m.group(3)})",
                    'description': m.group(7).strip(),
                }
                cont_lines = []

    # 文件结束，提交末尾待处理的 pending 条目
    if pending is not None:
        if cont_lines:
            pending['description'] = (
                pending['description'] + ' ' + ' '.join(cont_lines)
            ).strip()
        top_errors.append(pending)

    return {
        'file':       path.name,
        'filepath':   str(filepath),
        'statistics': statistics,
        'top_errors': top_errors,
    }


def parse_logs(filepaths: list) -> list:
    """并行解析多个日志文件，返回结果列表（顺序与输入一致）。"""
    if len(filepaths) == 1:
        return [parse_log(filepaths[0])]
    with ThreadPoolExecutor() as executor:
        return list(executor.map(parse_log, filepaths))
