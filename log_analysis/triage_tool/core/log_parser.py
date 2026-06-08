# -*- coding: utf-8 -*-
import re
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor


# UVM 报错行通用正则（IEEE 1800.2 default report server + 常见自定义 server 兼容）
# 覆盖的变体（见 BUGLOG.md BUG-029）：
#   1. 标准:        UVM_ERROR /tb/dut.sv(42) @ 100ns: comp [ID] msg
#   2. 缺 file:     UVM_ERROR @ 82.00ns: comp [ID] msg              （sequence/vsequencer 报错）
#   3. time 带空格: UVM_ERROR ... @ 0 ps: ... / @ 6933.414503 us: ...
#   4. OpenTitan:   UVM_FATAL @ 0 ps: (file.sv:161) [ral] msg       （reporter 槽吞 (file:line)）
#   5. verbosity:   UVM_ERROR(MEDIUM) ...                            （show_verbosity=1）
#   6. 参数化 id:   [uvm_driver #(REQ,RSP)]                          （含空格/#/(),）
#   7. 空 id:       []                                               （规范合法的边角）
# Group 用命名组以避免编号漂移；file/line/reporter 在缺失变体下为 None。
_UVM_PATTERN = re.compile(
    r'(?P<level>UVM_(?:ERROR|WARNING|FATAL))'          # severity
    r'(?:\([^)]*\))?'                                  # 可选 verbosity 后缀 (MEDIUM)
    r'(?:\s+(?P<file>\S+)\((?P<line>\d+)\))?'          # 可选 file(line)
    r'\s+@\s*(?P<time>[\d.]+(?:\s*[a-z]+s)?)'          # time（数字 + 可选单位，单位前可有空格）
    r'\s*:\s*'
    r'(?:(?P<reporter>\S+)\s+)?'                       # 可选 reporter / hier_path
    r'\[(?P<id>[^\]]*)\]\s*'                           # id（允许空 + 空格 + 特殊字符）
    r'(?P<msg>.*)',                                    # 描述
    re.IGNORECASE
)

# 匹配任意UVM行（含INFO），用于续行检测时排除
_UVM_ANY = re.compile(r'UVM_(?:ERROR|WARNING|FATAL|INFO)\s', re.IGNORECASE)

# Synopsys VCS 标准报错正则（见 BUGLOG.md BUG-031）
# 主格式：Error-[CNST-CIF] Constraints inconsistency failure
# severity 字面量：Error / Warning / Fatal / Note / Info（IC 验证语境下 Note/Info 不计入错误统计）
# ID 字符集：[A-Z][A-Z0-9_-]*（含连字符，如 VPI-CT-NS / DPI-UED）
# 续行：与 UVM 一致的 indented 策略（2-space 缩进）
# 真实样本来源：opentitan/SpinalHDL/chipyard GitHub issues
_VCS_PATTERN = re.compile(
    r'^(?P<level>Error|Warning|Fatal|Note|Info)'   # severity 字面量
    r'-\['
    r'(?P<id>[A-Z][A-Z0-9_-]*)'                    # ID（含连字符）
    r'\]\s*'
    r'(?P<msg>.*)',
    re.IGNORECASE
)

# 匹配任意 VCS 行（含 Note/Info），用于续行检测时排除
_VCS_ANY = re.compile(r'^(?:Error|Warning|Fatal|Note|Info)-\[', re.IGNORECASE)

# VCS severity → 内部统计字段映射（Note/Info 不计错误，不映射）
_VCS_LEVEL_MAP = {
    'ERROR':   'ERROR',
    'WARNING': 'WARNING',
    'FATAL':   'FATAL',
}

# Cadence Xcelium 标准报错正则（见 BUGLOG.md BUG-032）
# 主格式：xmsim: *E,XYZID (file.sv,142): error description
# 工具前缀全集：xrun (Xcelium 主) / xmsim/xmelab/xmvlog/xmverilog/xmsd (Xcelium 子工具)
#               / irun (Incisive，已被 xrun 取代) / ncsim/ncelab/ncvlog (旧 NC-Verilog)
# severity 字面量：*E (Error) / *W (Warning) / *F (Fatal) / *SE (Severe Error) / *N (Note) / *I (Info)
#                  注意 *SE 是双字符，正则用 \*[A-Z]+
# ID 字符集：[A-Z][A-Z0-9_]*（含数字，如 DSEM2009 / MBXNYI / VLGERR；**不含连字符**，跟 VCS 不同）
# 可选 source location：(file,line) 或 (file,line|column)
# 真实样本来源：cocotb/openhwgroup/cva6/riscv-dv GitHub issues
_XCELIUM_PATTERN = re.compile(
    r'^(?P<tool>xrun|xmsim|xmelab|xmvlog|xmverilog|xmsd|ncsim|ncelab|ncvlog|irun)'
    r'(?:\(\d+\))?'                                          # 可选 (PID/版本) 后缀，如 xrun(64)
    r':\s*\*(?P<level>[A-Z]+),'                              # *severity, （[A-Z]+ 覆盖 *SE 双字符）
    r'(?P<id>[A-Z][A-Z0-9_]*)'                               # ID（仅字母数字下划线）
    r'(?:\s*\((?P<file>[^,)]+),(?P<line>\d+)(?:\|\d+)?\))?'  # 可选 (file,line) 或 (file,line|col)
    r':\s*(?P<msg>.*)',
    re.IGNORECASE
)

# 匹配任意 Xcelium 行（用于续行检测时排除）
_XCELIUM_ANY = re.compile(
    r'^(?:xrun|xmsim|xmelab|xmvlog|xmverilog|xmsd|ncsim|ncelab|ncvlog|irun)'
    r'(?:\(\d+\))?:\s*\*',
    re.IGNORECASE
)

# Xcelium severity → 内部统计字段映射（N/I/D/P 等不计错误，不映射）
_XCELIUM_LEVEL_MAP = {
    'E':  'ERROR',
    'W':  'WARNING',
    'F':  'FATAL',
    'SE': 'ERROR',   # Severe Error 归并到 ERROR
}

TOP_N = 5  # 每个日志最多提取的错误条数（按出现顺序）


def _error_result(filepath: str, error_msg: str) -> dict:
    """解析失败时返回带 error 字段的占位结果，供调用方识别并继续处理其余文件。"""
    return {
        'file':       Path(filepath).name,
        'filepath':   str(filepath),
        'statistics': {'UVM_WARNING': 0, 'UVM_ERROR': 0, 'UVM_FATAL': 0},
        'status':     'fail',
        'pass_found': False,
        'top_errors': [],
        'all_errors': [],
        'error':      error_msg,
    }


def _build_gen_pattern(keywords):
    """构建行首关键词匹配正则，覆盖三类 IC 仿真器输出（见 BUGLOG BUG-030）：
      - VCS 标准报错:    Error-[CNST-CIF] Constraints inconsistency failure
      - IP 内部报错:     IP_FATAL[T_BUS_ERR] timeout on cycle 91
      - SVA 自定义:      MY_SVA_ERR signal X stayed low for 100ns
      - 兼容旧格式:       ERROR: classic colon style
    分组：G1 关键词（=level，user-configured），G2 可选 [ID]（parser-extracted），
          G3 描述。关键词字面量与 level 一一对应；ID 可选，无则走 KB Step2 关键词匹配。
    keywords 为空列表时返回 None。
    """
    if not keywords:
        return None
    alts = '|'.join(re.escape(kw) for kw in keywords)
    return re.compile(
        r'^(' + alts + r')\b'                # G1: 关键词 + word boundary（防 Erroring 半匹配）
        r'[\s:\-]*'                          # 任意分隔符（空白/冒号/连字符，0+ 个）
        r'(?:\[([^\]]+)\])?'                 # G2: 可选 [ID]
        r'\s*(.*)',                          # G3: 描述
        re.IGNORECASE
    )


def parse_log(filepath: str, extra_keywords=None, pass_patterns=None) -> dict:
    """
    逐行流式解析单个仿真日志文件，返回结构化结果。
    内存占用与文件大小无关，仅保留当前处理窗口（续行缓冲最多3行）。
    top_errors: 按出现顺序提取前 TOP_N 条错误，每条尝试合并后续续行描述。
    extra_keywords: 额外支持的行首关键词列表（如 ['ERROR', 'FATAL', 'FAILED']）。
    pass_patterns: 通过标记字符串列表（如 ['JVP TEST PASSED']），任意一条出现即视为找到通过标记。
                   非空时：PASS = 无错误 && 找到通过标记；空时退化为旧逻辑（只看有无错误）。
    """
    path = Path(filepath)
    extra_keywords = [kw.upper() for kw in (extra_keywords or [])]
    pass_patterns  = list(pass_patterns or [])

    statistics = {'UVM_WARNING': 0, 'UVM_ERROR': 0, 'UVM_FATAL': 0}
    for kw in extra_keywords:
        statistics.setdefault(kw, 0)

    _gen_pattern = _build_gen_pattern(extra_keywords)
    pass_found   = False   # 是否在文件中检测到通过标记

    top_errors = []
    pending = None    # 等待续行收集的当前条目（仅 top_errors 未满时使用）
    cont_lines = []   # 已收集的续行文本
    all_errors  = []  # 全文去重错误列表（含 WARNING，每个唯一 error_id 只记一次）
    _seen_keys  = set()

    with open(str(path), encoding='utf-8', errors='replace') as f:
        for raw_line in f:
            line = raw_line.rstrip('\n')
            stripped = line.strip()

            # ── 通过标记检测（全文扫描，任意行匹配即记录） ────────
            if not pass_found and pass_patterns:
                if any(p in line for p in pass_patterns):
                    pass_found = True

            # ── 续行收集 ──────────────────────────────────────────
            if pending is not None:
                if (stripped
                        and not _UVM_ANY.search(stripped)
                        and not _VCS_ANY.match(stripped)
                        and not _XCELIUM_ANY.match(stripped)
                        and line.startswith(' ')
                        and len(cont_lines) < 3):
                    cont_lines.append(stripped)
                    continue
                # 续行终止（遇到空行 / UVM/VCS/Xcelium 条目 / 非缩进行 / 已满3行）：提交 pending
                if cont_lines:
                    pending['description'] = (
                        pending['description'] + ' ' + ' '.join(cont_lines)
                    ).strip()
                top_errors.append(pending)
                pending = None
                cont_lines = []
                # 当前行继续向下走，检查是否为新的 UVM / VCS / Xcelium / 通用关键词条目

            # ── UVM 条目匹配（优先） ───────────────────────────────
            m = _UVM_PATTERN.search(line)
            if m:
                level = m.group('level').upper()
                if level in statistics:
                    statistics[level] += 1

                _err_id   = m.group('id').strip()
                _msg      = m.group('msg').strip()
                _file     = m.group('file')
                _line_no  = m.group('line')
                _location = f"{_file}({_line_no})" if _file else ''

                # 全量去重记录（含 WARNING）：相同 level+error_id 只保留首次出现
                _dup_key = (level, _err_id.lower() if _err_id
                            else _msg[:80].lower())
                if _dup_key not in _seen_keys:
                    _seen_keys.add(_dup_key)
                    all_errors.append({
                        'level':       level,
                        'error_id':    _err_id,
                        'description': _msg,
                        'location':    _location,
                    })

                # WARNING 仅统计，不计入 top_errors；FATAL/ERROR 才参与匹配
                if level == 'UVM_WARNING':
                    continue

                # top_errors 未满时才记录条目（仍需对全文统计错误数）
                if len(top_errors) < TOP_N:
                    pending = {
                        'level':       level,
                        'timestamp':   m.group('time').replace(' ', ''),
                        'error_id':    _err_id,
                        'location':    _location,
                        'description': _msg,
                    }
                    cont_lines = []
                continue

            # ── VCS 条目匹配（UVM 未命中时） ──────────────────────
            vm = _VCS_PATTERN.match(stripped)
            if vm:
                raw_level = vm.group('level').upper()
                # Note/Info 不计入错误统计、不进 top_errors
                if raw_level in _VCS_LEVEL_MAP:
                    level = _VCS_LEVEL_MAP[raw_level]
                    statistics[level] = statistics.get(level, 0) + 1

                    _err_id = vm.group('id').strip()
                    _msg    = vm.group('msg').strip()

                    _dup_key = (level, _err_id.lower() if _err_id
                                else _msg[:80].lower())
                    if _dup_key not in _seen_keys:
                        _seen_keys.add(_dup_key)
                        all_errors.append({
                            'level':       level,
                            'error_id':    _err_id,
                            'description': _msg,
                            'location':    '',
                        })

                    # WARNING 仅统计，不进 top_errors（跟 UVM_WARNING 一致）
                    if level == 'WARNING':
                        continue

                    if len(top_errors) < TOP_N:
                        pending = {
                            'level':       level,
                            'timestamp':   '',
                            'error_id':    _err_id,
                            'location':    '',
                            'description': _msg,
                        }
                        cont_lines = []
                continue

            # ── Cadence Xcelium 条目匹配（UVM/VCS 未命中时） ──────
            xm = _XCELIUM_PATTERN.match(stripped)
            if xm:
                raw_level = xm.group('level').upper()
                # *N / *I / *D / *P 等不映射，跳过（IC 验证语境下非错误）
                if raw_level in _XCELIUM_LEVEL_MAP:
                    level = _XCELIUM_LEVEL_MAP[raw_level]
                    statistics[level] = statistics.get(level, 0) + 1

                    _err_id   = xm.group('id').strip()
                    _msg      = xm.group('msg').strip()
                    _file     = xm.group('file')
                    _line_no  = xm.group('line')
                    _location = f"{_file}({_line_no})" if _file else ''

                    _dup_key = (level, _err_id.lower() if _err_id
                                else _msg[:80].lower())
                    if _dup_key not in _seen_keys:
                        _seen_keys.add(_dup_key)
                        all_errors.append({
                            'level':       level,
                            'error_id':    _err_id,
                            'description': _msg,
                            'location':    _location,
                        })

                    # WARNING 仅统计，不进 top_errors（跟 UVM_WARNING / VCS Warning 一致）
                    if level == 'WARNING':
                        continue

                    if len(top_errors) < TOP_N:
                        pending = {
                            'level':       level,
                            'timestamp':   '',
                            'error_id':    _err_id,
                            'location':    _location,
                            'description': _msg,
                        }
                        cont_lines = []
                continue

            # ── 通用行首关键词匹配（UVM/VCS/Xcelium 未命中时） ────
            if _gen_pattern:
                mg = _gen_pattern.match(stripped)
                if mg:
                    level       = mg.group(1).upper()
                    _err_id     = (mg.group(2) or '').strip()   # 可选 [ID]
                    description = mg.group(3).strip()
                    statistics[level] = statistics.get(level, 0) + 1

                    _dup_key = (level, _err_id.lower() if _err_id
                                else description[:80].lower())
                    if _dup_key not in _seen_keys:
                        _seen_keys.add(_dup_key)
                        all_errors.append({
                            'level':       level,
                            'error_id':    _err_id,
                            'description': description,
                            'location':    '',
                        })

                    if len(top_errors) < TOP_N:
                        pending = {
                            'level':       level,
                            'timestamp':   '',
                            'error_id':    _err_id,  # 有则走 Step1 精确匹配，无则降级 Step2
                            'location':    '',
                            'description': description,
                        }
                        cont_lines = []

    # 文件结束，提交末尾待处理的 pending 条目
    if pending is not None:
        if cont_lines:
            pending['description'] = (
                pending['description'] + ' ' + ' '.join(cont_lines)
            ).strip()
        top_errors.append(pending)

    # pass/fail 判断
    # 非 WARNING 类型的计数若任意 > 0 则视为有错误
    non_warning = {k: v for k, v in statistics.items() if 'WARNING' not in k.upper()}
    has_error   = any(v > 0 for v in non_warning.values())
    if pass_patterns:
        # 配置了通过标记：PASS = 无错误 && 找到通过标记
        # FAIL = 有错误 || 无错误但未找到通过标记
        status = 'pass' if (not has_error and pass_found) else 'fail'
    else:
        # 未配置通过标记：退化为旧逻辑，只看有无错误
        status = 'pass' if not has_error else 'fail'

    return {
        'file':       path.name,
        'filepath':   str(filepath),
        'statistics': statistics,
        'status':     status,
        'pass_found': pass_found,
        'top_errors': top_errors,
        'all_errors': all_errors,
    }


def parse_logs(filepaths: list, progress_cb=None,
               extra_keywords=None, pass_patterns=None) -> list:
    """并行解析多个日志文件，返回结果列表（顺序与输入一致）。
    progress_cb(filename, result, done, total) — 每完成一个文件后调用。
    extra_keywords: 额外行首关键词列表，传给每个 parse_log 调用。
    pass_patterns:  通过标记字符串列表，传给每个 parse_log 调用。
    """
    from concurrent.futures import as_completed
    total = len(filepaths)
    if total == 1:
        result = parse_log(filepaths[0],
                           extra_keywords=extra_keywords,
                           pass_patterns=pass_patterns)
        if progress_cb:
            progress_cb(Path(filepaths[0]).name, result, 1, 1)
        return [result]
    results = {}
    with ThreadPoolExecutor() as executor:
        future_to = {
            executor.submit(parse_log, fp, extra_keywords, pass_patterns): (i, fp)
            for i, fp in enumerate(filepaths)
        }
        done = 0
        for future in as_completed(future_to):
            i, fp = future_to[future]
            try:
                r = future.result()
            except Exception as e:
                r = _error_result(fp, str(e))
            results[i] = r
            done += 1
            if progress_cb:
                progress_cb(Path(fp).name, r, done, total)
    return [results[i] for i in range(total)]
