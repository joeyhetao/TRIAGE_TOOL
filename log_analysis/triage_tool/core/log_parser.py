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
    all_errors  = []  # 全文去重错误列表（含 WARNING，每个唯一 error_id 只记一次）
    _seen_keys  = set()

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

            # 全量去重记录（含 WARNING）：相同 level+error_id 只保留首次出现
            _err_id  = m.group(6).strip()
            _dup_key = (level, _err_id.lower() if _err_id
                        else m.group(7).strip()[:80].lower())
            if _dup_key not in _seen_keys:
                _seen_keys.add(_dup_key)
                all_errors.append({
                    'level':       level,
                    'error_id':    _err_id,
                    'description': m.group(7).strip(),
                    'location':    f"{m.group(2)}({m.group(3)})",
                })

            # WARNING 仅统计，不计入 top_errors；FATAL/ERROR 才参与匹配
            if level == 'UVM_WARNING':
                continue

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

    # PASS: 无 UVM_ERROR 且无 UVM_FATAL；FAIL: 任意一条即为 FAIL
    status = 'pass' if statistics['UVM_ERROR'] == 0 and statistics['UVM_FATAL'] == 0 else 'fail'

    return {
        'file':       path.name,
        'filepath':   str(filepath),
        'statistics': statistics,
        'status':     status,
        'top_errors': top_errors,
        'all_errors': all_errors,
    }


def parse_logs(filepaths: list, progress_cb=None) -> list:
    """并行解析多个日志文件，返回结果列表（顺序与输入一致）。
    progress_cb(filename, result, done, total) — 每完成一个文件后调用。
    """
    from concurrent.futures import as_completed
    total = len(filepaths)
    if total == 1:
        result = parse_log(filepaths[0])
        if progress_cb:
            progress_cb(Path(filepaths[0]).name, result, 1, 1)
        return [result]
    results = {}
    with ThreadPoolExecutor() as executor:
        future_to = {executor.submit(parse_log, fp): (i, fp)
                     for i, fp in enumerate(filepaths)}
        done = 0
        for future in as_completed(future_to):
            i, fp = future_to[future]
            r = future.result()
            results[i] = r
            done += 1
            if progress_cb:
                progress_cb(Path(fp).name, r, done, total)
    return [results[i] for i in range(total)]
