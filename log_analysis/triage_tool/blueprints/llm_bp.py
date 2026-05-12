# -*- coding: utf-8 -*-
"""
LLM 功能 Blueprint — 所有 AI 辅助路由。

路由列表（共 18 条）：
  POST /llm/reload_config      P0: 热重载配置
  GET  /llm/get_config         P0: 获取当前配置（api_key 脱敏，含 profile 列表）
  POST /llm/save_config        P0: 更新当前激活 profile（含运行任务检测）
  POST /llm/test_connection    P0: 连接测试
  POST /llm/profile/add        P0: 新增 profile
  POST /llm/profile/update     P0: 更新指定 profile（含 rename）
  POST /llm/profile/delete     P0: 删除 profile（拒绝最后一个）
  POST /llm/profile/activate   P0: 切换激活 profile（含运行任务检测）
  POST /llm/rank_entries       P1: 多条匹配智能推荐
  POST /llm/custom_extract     P2: 自定义提取（按 file_index 切文件，多轮对话；上传/路径模式均支持）
  POST /llm/similar_errors     P3: 相似错误推荐（写回辅助）
  POST /llm/batch_patterns     P4: 批量错误模式分析
  POST /llm/semantic_query     P5: 语义知识库查询重排
  POST /llm/kb_review          P6: 启动知识库质量检查（后台任务）
  GET  /llm/kb_review_status   P6: 查询检查进度
  POST /llm/kb_review_stop     P6: 停止检查
  GET  /llm/kb_review_export   P6: 导出检查结果 Excel
  POST /llm/merge_suggest      P6: AI 建议合并两条重复条目
"""
import re
import io
import json
import time
import uuid
import threading
from pathlib import Path
from flask import Blueprint, request, jsonify, current_app, send_file

import state
from core import llm_client
from core.db_manager import load_db
from core.matcher import score_query

llm_bp = Blueprint('llm', __name__)

# ── P6 后台任务存储（独立于 state._jobs，字段结构不同） ──────
_review_jobs = {}        # job_id -> {...}
_review_lock = threading.Lock()
_REVIEW_JOB_TTL = 3600  # 1 小时后自动清理

# ══════════════════════════════════════════════════════════
# 内部常量 / 辅助函数
# ══════════════════════════════════════════════════════════

_P3_SYSTEM_PROMPT = (
    '你是一名芯片验证工程师助手，专注于 UVM 仿真日志分析。\n\n'
    '严格规则（必须遵守）：\n'
    '1. 只能基于下方「日志内容」回答，不得推断、补充或编造日志中不存在的信息\n'
    '2. 如果所问信息在日志中找不到，必须明确回答「日志中未找到相关内容」，不得猜测\n'
    '3. 每条结论后必须标注原始行号，格式：（L行号） 或 L行号'
)

# 匹配真实 UVM 错误行：含时间戳 @ 或文件路径（排除末尾汇总统计行）
_UVM_SCAN_PAT = re.compile(r'\bUVM_(?:ERROR|WARNING|FATAL)\b')
_UVM_REAL_PAT = re.compile(r'\bUVM_(?:ERROR|WARNING|FATAL)\b.*@')

P3_OVERHEAD_TOKENS = 800   # System Prompt + User Message 模板 + 多轮历史估算

# P2 prescan 锚点上限（M-4, 2026-05-11 审查）：
# 原实现把每个命中行的权重塞 dict，文件越大、关键词越通用，dict 越爆炸。
# 现在 cap 在 20 万锚点（~16 MB 内存上限），超出后停止累加（保留前 N 个最密集区段
# 的精度）。last_anchor_overall 仍跟踪完整文件末锚点，保证 _query_prefers_end
# 分支不被截断影响。
_MAX_PRESCAN_ANCHORS = 200_000


# 中文停用词：常见虚词 + 元查询词（"列出""为什么"等只描述意图，不是真锚点）
_CN_STOPWORDS = frozenset([
    '的', '和', '与', '及', '或', '但', '了', '是', '在', '有', '被', '把', '将', '并',
    '对', '为', '从', '到', '给', '让', '使', '请', '一个', '一些', '这个', '这些',
    '所有', '列出', '汇总', '统计', '分析', '描述', '出现', '发生', '看看', '查看',
    '什么', '为什么', '怎么', '如何', '哪些', '哪个', '是否', '能否', '可否',
    '日志', '内容', '信息', '里面', '中间', '附近', '前后', '上下文',
])

# 中文 → 英文同义词映射：让纯中文 query 也能匹配到日志中的英文 UVM token
_CN_SYNONYMS = {
    '报错':   ['error', 'err', 'fatal', 'warning'],
    '错误':   ['error', 'err'],
    '致命':   ['fatal'],
    '严重':   ['fatal', 'severe'],
    '警告':   ['warning', 'warn'],
    '失败':   ['fail', 'failure', 'fault'],
    '崩溃':   ['crash', 'abort', 'segfault'],
    '超时':   ['timeout', 'timed out'],
    '断言':   ['assert', 'assertion'],
    '复位':   ['reset', 'rst'],
    '时钟':   ['clock', 'clk'],
    '配置':   ['config', 'cfg', 'setting'],
    '初始化': ['init', 'initialize', 'reset'],
    '启动':   ['start', 'startup', 'launch', 'boot'],
    '驱动':   ['driver', 'drv'],
    '检查器': ['checker', 'chk'],
    '监视器': ['monitor', 'mon'],
    '记分板': ['scoreboard', 'sb', 'scb'],
    '测试':   ['test', 'tc', 'testcase'],
    '环境':   ['env', 'environment'],
    '事务':   ['txn', 'transaction', 'trans'],
    '阶段':   ['phase'],
    '寄存器': ['reg', 'register'],
    '总线':   ['bus'],
    '中断':   ['interrupt', 'irq', 'intr'],
    '溢出':   ['overflow', 'over'],
    '欠溢':   ['underflow', 'under'],
    '握手':   ['handshake'],
    '通道':   ['channel', 'chan'],
    '地址':   ['addr', 'address'],
    '数据':   ['data'],
    '使能':   ['enable', 'en'],
    '有效':   ['valid', 'vld'],
    '就绪':   ['ready', 'rdy'],
}


def _extract_query_keywords(query: str) -> list:
    """
    从查询文本中提取关键词，用于预扫描锚定。
    覆盖：引号短语、ALL_CAPS 错误 ID、英文/下划线 token、中文连续片段、中→英同义词。
    """
    kws = []
    kws += re.findall(r'"([^"]+)"', query)                           # 引号内短语
    kws += re.findall(r'\b[A-Z][A-Z0-9_]{2,}\b', query)                          # 形似错误ID（大写）
    stripped = re.sub(r'"[^"]*"', ' ', query)
    stripped = re.sub(r'\b[A-Z][A-Z0-9_]{2,}\b', ' ', stripped)

    # 英文 / 下划线 token（如 uvm_error）—— 按中文/数字/空白切分后取剩余 token
    for w in re.split(r'[\s\d一-鿿＀-￯]+', stripped):
        w = w.strip('_- ')
        if len(w) > 2:
            kws.append(w)

    # 中文连续片段（≥2 字）+ 滑窗子片段 + 同义词扩展。
    # 锚点扫描是 substring match：query 里"报错"能匹配日志中"报错原因"；
    # 同义词扩出 'error'/'fatal' 等英文 token 帮匹配 UVM 行。
    _CN_PARTICLES = '的了是在有被把将并对为从到给让使请和与及或但'  # 单字虚词
    for chunk in re.findall(r'[一-鿿]{2,}', query):
        candidates = {chunk}
        for size in (2, 3, 4):
            for i in range(len(chunk) - size + 1):
                candidates.add(chunk[i:i + size])
        for c in candidates:
            if c in _CN_STOPWORDS:
                continue
            # 过滤：以单字虚词开头/结尾的子片段（"的报"、"分析最"等噪声）
            if c[0] in _CN_PARTICLES or c[-1] in _CN_PARTICLES:
                continue
            # 过滤：整体是停用词的前缀/后缀扩展（"分析最"含"分析"，已是噪声）
            if any(c.startswith(sw) or c.endswith(sw)
                   for sw in _CN_STOPWORDS if len(sw) >= 2 and sw != c):
                continue
            kws.append(c)
            if c in _CN_SYNONYMS:
                kws.extend(_CN_SYNONYMS[c])

    return list(dict.fromkeys(kws))   # 去重保序


def _get_format_instruction(query: str) -> tuple:
    """根据查询意图自动判断返回格式。返回 (format_type, instruction_str)。"""
    list_kws = ['列出', '汇总', '统计', '所有', '枚举', 'list', 'all', 'summary', 'enumerate']
    text_kws = ['分析', '描述', '发生', '为什么', '解释', '原因', 'why', 'analyze', 'explain']
    if any(k in query for k in list_kws):
        return ('json',
                '请以 JSON 数组返回，每条包含必要字段和 `line` 行号字段。'
                '只包含日志中明确出现的值，缺失字段填 null。')
    if any(k in query for k in text_kws):
        return ('text',
                '请用中文描述，每个结论后用（L行号）标注日志原文出处。')
    return ('text',
            '请根据查询意图选择合适格式（JSON 或文字），所有结论必须标注行号来源。')


_PREFER_START_PAT = re.compile(
    r'前\s*\d|第\s*[一1]|^first|^top\s*\d|earliest|最早|最前|'
    r'初始化|开头|起始|开始|启动|first\s+few|at the beginning|from the start|'
    r'最先|刚开始',
    re.IGNORECASE)

_PREFER_END_PAT = re.compile(
    r'后\s*\d|最后|末尾|结尾|结束|尾部|临结束|失败时|失败前|崩溃前|'
    r'last\s+\d?|^last\b|final|^end\b|tail|最末|最终|刚结束',
    re.IGNORECASE)


def _query_prefers_start(query: str) -> bool:
    """判断查询是否意图获取文件开头的内容（前N个、第一个等）。"""
    return bool(_PREFER_START_PAT.search(query))


def _query_prefers_end(query: str) -> bool:
    """判断查询是否意图获取文件末尾的内容（最后N个、结束前等）。"""
    return bool(_PREFER_END_PAT.search(query))


def _p3_prescan(filepath: str, query: str, extra_patterns: list,
                max_lines: int) -> tuple:
    """
    单次流式扫描文件，给每行算锚点权重，用加权滑动窗口找密度最高的连续 max_lines 行区间。

    权重表（B + E）：
      UVM_FATAL（带 @）          : 5
      UVM_ERROR（带 @）          : 3
      UVM_WARNING（带 @）        : 1
      extra_patterns 命中        : +2
      查询关键词 substring 命中  : +6
      文件路径命中（kw.sv / /kw）: 在关键词命中基础上额外 +2

    若查询含"前N个/第一/初始化/开头/first/earliest"等意图，优先从首锚点开始；
    "最后/结束前/last" 等意图从末锚点回退。

    返回 (win_start, win_end, total_span, first_anchor, last_anchor, total_lines)
    所有行号均为 1-based。
    """
    keywords    = [kw.lower() for kw in _extract_query_keywords(query)]
    extra_lower = [p.lower() for p in extra_patterns]

    weights    = {}      # 所有锚点行号 -> 权重（UVM/extra/kw 全集）
    kw_weights = {}      # 仅"查询关键词命中"的子集 -> 权重
    total_lines = 0
    # M-4: 即使 dict 达上限不再累加，也跟踪整文件最早/最末锚点行号，
    # 供 prefers_start / prefers_end 分支与覆盖率提示使用。
    overall_first = 0
    overall_last  = 0
    kw_first      = 0
    kw_last       = 0

    try:
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            for lineno, line in enumerate(f, 1):
                total_lines = lineno
                ll = line.lower()
                w = 0
                if _UVM_REAL_PAT.search(line):
                    if 'UVM_FATAL' in line:
                        w += 5
                    elif 'UVM_ERROR' in line:
                        w += 3
                    else:
                        w += 1
                if any(p in ll for p in extra_lower):
                    w += 2
                kw_hit = False
                if keywords and any(kw in ll for kw in keywords):
                    w += 6
                    kw_hit = True
                if kw_hit:
                    for kw in keywords:
                        if kw + '.sv' in ll or '/' + kw in ll:
                            w += 2
                            break
                if w > 0:
                    if overall_first == 0:
                        overall_first = lineno
                    overall_last = lineno
                    if len(weights) < _MAX_PRESCAN_ANCHORS:
                        weights[lineno] = w
                if kw_hit:
                    if kw_first == 0:
                        kw_first = lineno
                    kw_last = lineno
                    if len(kw_weights) < _MAX_PRESCAN_ANCHORS:
                        kw_weights[lineno] = w
    except Exception:
        return 1, min(max_lines, 1), 0, 0, 0, 1

    # 若 query 关键词在文件中至少有一处命中，只在 kw 锚点中找窗口；否则用全集兜底。
    if kw_weights:
        active_weights = kw_weights
        overall_first  = kw_first
        overall_last   = kw_last

    else:
        active_weights = weights

    if not active_weights:
        end = min(max_lines, total_lines)
        return 1, end, 0, 0, 0, total_lines

    sorted_nos = sorted(active_weights.keys())
    # 锚点上限触发时，dict 内的极值可能不是整文件的极值，用 overall_* 兜底
    first_anchor = overall_first or sorted_nos[0]
    last_anchor  = overall_last  or sorted_nos[-1]
    total_span   = last_anchor - first_anchor + 1

    if total_span <= max_lines:
        # 所有锚点已在 max_lines 范围内，以首锚点前 50 行为起点
        win_start = max(1, first_anchor - 50)
        win_end   = min(total_lines, win_start + max_lines - 1)
        win_start = max(1, win_end - max_lines + 1)
        return win_start, win_end, total_span, first_anchor, last_anchor, total_lines

    # 若查询意图是"前N个/第一"，直接从首锚点开始，不做密度滑窗
    if _query_prefers_start(query):
        win_start = max(1, first_anchor - 50)
        win_end   = min(total_lines, win_start + max_lines - 1)
        return win_start, win_end, total_span, first_anchor, last_anchor, total_lines

    # 若查询意图是"最后N个/结束前"，从末锚点回退取段
    if _query_prefers_end(query):
        win_end   = min(total_lines, last_anchor + 50)
        win_start = max(1, win_end - max_lines + 1)
        return win_start, win_end, total_span, first_anchor, last_anchor, total_lines

    # 加权滑动窗口：找累计权重最大的连续 max_lines 行
    best_score = 0
    best_win_s = sorted_nos[0]
    left = 0
    cur_sum = 0
    for i, ra in enumerate(sorted_nos):
        win_s = ra - max_lines + 1
        cur_sum += active_weights[ra]
        while left < len(sorted_nos) and sorted_nos[left] < win_s:
            cur_sum -= active_weights[sorted_nos[left]]
            left += 1
        if cur_sum > best_score:
            best_score = cur_sum
            best_win_s = max(1, win_s)

    win_start = best_win_s
    win_end   = min(total_lines, win_start + max_lines - 1)
    win_start = max(1, win_end - max_lines + 1)
    return win_start, win_end, total_span, first_anchor, last_anchor, total_lines


# ══════════════════════════════════════════════════════════
# Phase 3 — 多块聚簇辅助
# ══════════════════════════════════════════════════════════

def _cluster_anchors(weights: dict, gap_threshold: int) -> list:
    """
    把锚点按行号 gap 切簇：连续锚点间距 > gap_threshold 时分簇。
    返回 [(cluster_start, cluster_end, total_weight), ...] 按起始行号升序。
    """
    if not weights:
        return []
    sorted_nos = sorted(weights.keys())
    clusters = []
    cur_start = sorted_nos[0]
    cur_end = sorted_nos[0]
    cur_weight = weights[cur_start]
    for ln in sorted_nos[1:]:
        if ln - cur_end > gap_threshold:
            clusters.append((cur_start, cur_end, cur_weight))
            cur_start = ln
            cur_end = ln
            cur_weight = weights[ln]
        else:
            cur_end = ln
            cur_weight += weights[ln]
    clusters.append((cur_start, cur_end, cur_weight))
    return clusters


def _allocate_blocks(clusters: list, max_lines: int, total_lines: int,
                     padding: int = 50, min_block: int = 100) -> list:
    """
    在 max_lines 预算内贪心选取簇生成块。按权重降序选，每块自然跨度 = 簇跨度 + 2×padding。
    超额时按块整体收缩，单簇过大会被截断。最后按起始行号排序，相邻/重叠块合并。
    返回 [(block_start, block_end, total_weight), ...]
    """
    if not clusters:
        return []
    by_weight = sorted(clusters, key=lambda c: -c[2])
    selected = []
    used = 0
    for cs, ce, cw in by_weight:
        block_span = (ce - cs + 1) + 2 * padding
        if used >= max_lines:
            break
        remaining = max_lines - used
        if block_span > remaining:
            if remaining < min_block:
                break
            block_span = remaining
        block_start = max(1, cs - padding)
        block_end   = min(total_lines, block_start + block_span - 1)
        block_start = max(1, block_end - block_span + 1)
        selected.append((block_start, block_end, cw))
        used += block_span
    if not selected:
        return []
    # 按起始行号排序后合并相邻/重叠块
    selected.sort(key=lambda b: b[0])
    merged = [selected[0]]
    for s, e, w in selected[1:]:
        ms, me, mw = merged[-1]
        if s <= me + 1:
            merged[-1] = (ms, max(me, e), mw + w)
        else:
            merged.append((s, e, w))
    return merged


def _p3_prescan_blocks(filepath: str, query: str, extra_patterns: list,
                       max_lines: int) -> tuple:
    """
    扫描文件 + 多块取段。基于权重表（B+E）+ 聚簇（C）+ 意图分支（D）。
    返回 (blocks, first_anchor, last_anchor, total_lines)
      blocks = [(start, end, weight)]，至少 1 块（无锚点时是首 max_lines）。
    """
    keywords    = [kw.lower() for kw in _extract_query_keywords(query)]
    extra_lower = [p.lower() for p in extra_patterns]

    weights    = {}
    kw_weights = {}
    total_lines = 0
    # M-4: 完整文件范围内的最早/最末锚点（用于在 dict 达上限时兜底）
    overall_first = 0
    overall_last  = 0
    kw_first      = 0
    kw_last       = 0

    try:
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            for lineno, line in enumerate(f, 1):
                total_lines = lineno
                ll = line.lower()
                w = 0
                if _UVM_REAL_PAT.search(line):
                    if 'UVM_FATAL' in line:
                        w += 5
                    elif 'UVM_ERROR' in line:
                        w += 3
                    else:
                        w += 1
                if any(p in ll for p in extra_lower):
                    w += 2
                kw_hit = False
                if keywords and any(kw in ll for kw in keywords):
                    w += 6
                    kw_hit = True
                if kw_hit:
                    for kw in keywords:
                        if kw + '.sv' in ll or '/' + kw in ll:
                            w += 2
                            break
                if w > 0:
                    if overall_first == 0:
                        overall_first = lineno
                    overall_last = lineno
                    if len(weights) < _MAX_PRESCAN_ANCHORS:
                        weights[lineno] = w
                if kw_hit:
                    if kw_first == 0:
                        kw_first = lineno
                    kw_last = lineno
                    if len(kw_weights) < _MAX_PRESCAN_ANCHORS:
                        kw_weights[lineno] = w
    except Exception:
        return [(1, min(max_lines, 1), 0)], 0, 0, 1

    if kw_weights:
        active = kw_weights
        overall_first = kw_first
        overall_last  = kw_last
    else:
        active = weights
    if not active:
        end = min(max_lines, total_lines)
        return [(1, end, 0)], 0, 0, total_lines

    sorted_nos = sorted(active.keys())
    first_anchor = overall_first or sorted_nos[0]
    last_anchor  = overall_last  or sorted_nos[-1]

    gap_threshold = max(max_lines // 4, 50)
    clusters = _cluster_anchors(active, gap_threshold)

    # 意图分支（D）：START / END 限定簇
    prefers_start = _query_prefers_start(query)
    prefers_end   = _query_prefers_end(query)
    if prefers_start and not prefers_end and clusters:
        clusters = clusters[:1]
    elif prefers_end and not prefers_start and clusters:
        clusters = clusters[-1:]
    elif prefers_start and prefers_end and len(clusters) >= 2:
        # 同时含两端意图：保留首末两簇
        clusters = [clusters[0], clusters[-1]]

    blocks = _allocate_blocks(clusters, max_lines, total_lines)
    if not blocks:
        end = min(max_lines, total_lines)
        return [(1, end, 0)], first_anchor, last_anchor, total_lines
    return blocks, first_anchor, last_anchor, total_lines



def _read_lines_range(filepath: str, start: int, end: int) -> list:
    """读取文件指定行范围（1-based），返回 [(lineno, content), ...]。"""
    lines = []
    try:
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            for lineno, line in enumerate(f, 1):
                if lineno > end:
                    break
                if lineno >= start:
                    lines.append((lineno, line.rstrip('\n')))
    except Exception:
        pass
    return lines


def _format_log_lines(lines: list) -> str:
    return '\n'.join(f'L{ln} | {content}' for ln, content in lines)


def _apply_token_budget(lines: list, cfg: dict,
                        win_start: int, win_end: int) -> tuple:
    """
    若内容超出 token 预算，以中心对称收缩窗口端点（不跳行）。
    返回 (trimmed_lines, new_start, new_end, was_trimmed)。
    """
    ctx = int(cfg.get('context_window', 100000))
    cpt = float(cfg.get('p3_chars_per_token', 4))
    max_chars = int((ctx - P3_OVERHEAD_TOKENS) * cpt)

    if not lines:
        return lines, win_start, win_end, False

    content_len = sum(len(f'L{ln} | {c}') + 1 for ln, c in lines)
    if content_len <= max_chars:
        return lines, win_start, win_end, False

    avg       = content_len / len(lines)
    safe_cnt  = max(1, int(max_chars / avg))
    excess    = len(lines) - safe_cnt
    trim_f    = excess // 2
    trim_b    = excess - trim_f

    trimmed   = lines[trim_f: (len(lines) - trim_b) if trim_b else None]
    new_start = trimmed[0][0]  if trimmed else win_start
    new_end   = trimmed[-1][0] if trimmed else win_end
    return trimmed, new_start, new_end, True


# 启发式常量
_AVG_LINE_CHARS = 100   # UVM 行典型字符数（粗估）
_OUTPUT_RESERVE = 0.7   # 给 LLM 输出 + 多轮历史 + 系统提示留 30% 余量
_MIN_LINES_FLOOR = 100  # 即使 context 极小也至少给这么多行


def _validate_file_index(idx: int, n_files: int) -> bool:
    """检查 0 <= idx < n_files。n_files=0 时永远 False。"""
    return isinstance(idx, int) and 0 <= idx < n_files


def _adaptive_max_lines(cfg: dict) -> int:
    """
    根据 context_window 反推安全行数上限，再与用户配置 p3_max_lines 取小。

    既是"安全阀"（用户把 p3_max_lines 调到 50000 也不会真送爆 LLM），
    也是"自适应"（用户换更大 context 的模型时，自动放大；不必手动改）。

    估算公式：
      可用字符数 = (context_window - P3_OVERHEAD_TOKENS) × chars_per_token × _OUTPUT_RESERVE
      可装行数  = 可用字符数 / 平均行字符数
      返回值    = clamp(可装行数, _MIN_LINES_FLOOR, p3_max_lines)
    """
    ctx_tokens = int(cfg.get('context_window', 100000))
    cpt        = float(cfg.get('p3_chars_per_token', 4))
    user_cap   = int(cfg.get('p3_max_lines', 2500))

    safe_chars   = max(0, (ctx_tokens - P3_OVERHEAD_TOKENS)) * cpt * _OUTPUT_RESERVE
    fitting_lines = int(safe_chars / _AVG_LINE_CHARS) if _AVG_LINE_CHARS > 0 else 0
    return max(_MIN_LINES_FLOOR, min(fitting_lines, user_cap))


_FENCE_PAT = re.compile(r'```(?:json|JSON)?\s*\n?(.*?)```', re.DOTALL)


def _parse_json_safe(text: str, array: bool = False):
    """从 LLM 返回文本中提取首个完整 JSON 对象或数组。

    历史 bug H-1（2026-05-11 审查）：原实现用贪婪正则 ``r'\\{.*\\}'`` 抓
    "首 { 到末 }" 的最大区间，遇到 LLM 在 JSON 周围放解释/markdown/嵌套
    样例时退化为 None。这里改成括号配对状态机：先剥 ``​```json fence``，
    再从首个 ``{``/``[`` 起跟踪括号深度（处理字符串内的 ``"``/``\\\\``），
    在 depth 回到 0 时尝试 json.loads。
    """
    if not text:
        return None

    fence = _FENCE_PAT.search(text)
    if fence:
        text = fence.group(1)

    open_ch, close_ch = ('[', ']') if array else ('{', '}')
    start = text.find(open_ch)
    if start < 0:
        return None

    depth   = 0
    in_str  = False
    esc     = False
    for i in range(start, len(text)):
        c = text[i]
        if esc:
            esc = False
            continue
        if in_str:
            if c == '\\':
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
            continue
        if c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except Exception:
                    return None
    return None


def _cleanup_review_jobs():
    with _review_lock:
        now   = time.time()
        stale = [k for k, v in list(_review_jobs.items())
                 if now - v.get('ts', 0) > _REVIEW_JOB_TTL]
        for k in stale:
            del _review_jobs[k]


def _running_jobs_summary() -> list:
    """
    扫所有后台任务，返回正在运行的任务摘要列表。
    当前覆盖：P6 知识库质检（_review_jobs）。
    每条返回 {'kind', 'job_id', 'phase', 'progress'}。

    注意：P6 review job 在 _review_jobs 里用 'status' 字段（不是 'phase'）。
    历史 bug C-1（2026-05-11 审查）：之前这里读 'phase' 导致永远拿不到
    P6 任务，profile 切换互锁完全失效——务必读 'status'。
    """
    summary = []
    with _review_lock:
        for jid, job in _review_jobs.items():
            status = job.get('status', '')
            if status and status not in ('done', 'error', 'stopped'):
                done  = job.get('done', 0)
                total = max(job.get('total', 1), 1)
                summary.append({
                    'kind':     'P6_kb_review',
                    'job_id':   jid,
                    'phase':    status,                         # 前端兼容字段名仍叫 phase
                    'progress': int(done * 100 / total),
                })
    return summary


# ══════════════════════════════════════════════════════════
# P0 — 配置热重载
# ══════════════════════════════════════════════════════════

@llm_bp.route('/llm/reload_config', methods=['POST'])
def reload_config():
    ok  = llm_client.reload_config()
    current_app.jinja_env.globals['llm_enabled'] = ok
    cfg = llm_client.get_config()
    return jsonify({'ok': True, 'llm_enabled': ok,
                    'model': cfg.get('model', '') if cfg else ''})


@llm_bp.route('/llm/test_connection', methods=['POST'])
def test_connection():
    """发送最小请求验证 LLM 端点连通性，返回延迟、首词和详细错误。"""
    if not llm_client.is_configured():
        return jsonify({'ok': False, 'reason': 'LLM 未配置，请先填写配置'})
    messages = [{'role': 'user', 'content': '请回复"OK"'}]
    t0 = time.time()
    result, error = llm_client.call_llm_verbose(messages, temperature=0, max_tokens=20)
    elapsed = round((time.time() - t0) * 1000)
    cfg = llm_client.get_config() or {}
    if not result:
        return jsonify({'ok': False,
                        'reason': error or 'LLM 未返回内容，请检查端点/密钥/模型名称',
                        'elapsed_ms': elapsed})
    return jsonify({'ok': True, 'elapsed_ms': elapsed,
                    'model': cfg.get('model', ''),
                    'reply': result[:80]})


# ══════════════════════════════════════════════════════════
# P0 — 获取当前配置（api_key 脱敏，含 profile 列表）
# ══════════════════════════════════════════════════════════

def _mask_api_key(raw: str) -> str:
    if raw and len(raw) > 4:
        return raw[:4] + '***'
    return '***' if raw else ''


def _profile_to_payload(p: dict) -> dict:
    """把内部 profile 转成响应格式（api_key 脱敏，仅暴露 UI 需要的字段）。"""
    return {
        'name':           p.get('name', ''),
        'endpoint':       p.get('endpoint', ''),
        'api_key':        _mask_api_key(p.get('api_key', '')),
        'model':          p.get('model', ''),
        'timeout':        p.get('timeout', 30),
        'context_window': p.get('context_window', 100000),
        'p3_max_lines':   p.get('p3_max_lines', 2500),
    }


@llm_bp.route('/llm/get_config', methods=['GET'])
def get_config():
    cfg = llm_client.get_config()
    configured = cfg is not None
    profiles = llm_client.get_all_profiles()
    active   = llm_client.get_active_profile_name()

    # 兼容字段：旧前端从 .config 读，新前端从 .profiles + .active_profile 读
    cur_payload = _profile_to_payload(cfg) if cfg else {}
    cur_payload.pop('name', None)   # 老 .config 不含 name 字段
    # H-3（2026-05-11）：若上次加载时 llm_config.json 解析失败，把原因传给前端
    load_error = llm_client.get_last_load_error()
    return jsonify({
        'ok':              True,
        'configured':      configured,
        'config':          cur_payload,                                      # 旧字段，激活 profile 快照
        'active_profile':  active,
        'profiles':        [_profile_to_payload(p) for p in profiles],
        'load_error':      load_error,
    })


# ══════════════════════════════════════════════════════════
# P0 — 保存配置（更新当前激活 profile）
# ══════════════════════════════════════════════════════════

def _validate_profile_payload(data: dict) -> tuple:
    """返回 (cfg, error)。cfg 含规整后的字段；error='' 表示通过。"""
    endpoint = (data.get('endpoint') or '').strip()
    model    = (data.get('model') or '').strip()
    if not endpoint:
        return None, 'endpoint 不能为空'
    if not endpoint.startswith('http'):
        return None, 'endpoint 必须以 http 开头'
    if not model:
        return None, '模型名称不能为空'
    try:
        cfg = {
            'endpoint':       endpoint,
            'api_key':        (data.get('api_key') or '').strip(),
            'model':          model,
            'timeout':        int(data.get('timeout', 30)),
            'context_window': int(data.get('context_window', 100000)),
            'p3_max_lines':   int(data.get('p3_max_lines', 2500)),
        }
    except (TypeError, ValueError):
        return None, '数值字段（timeout/context_window/p3_max_lines）必须为整数'
    name = (data.get('name') or '').strip()
    if name:
        cfg['name'] = name
    return cfg, ''


@llm_bp.route('/llm/save_config', methods=['POST'])
def save_config():
    """更新当前激活 profile。若有正在运行的 LLM 后台任务，禁止保存（除非 force）。"""
    data = request.get_json(force=True) or {}
    cfg, err = _validate_profile_payload(data)
    if err:
        return jsonify({'ok': False, 'reason': err})

    # 运行中任务检测——保存可能改变 endpoint/model，影响进行中的 LLM 调用
    if not data.get('force'):
        running = _running_jobs_summary()
        if running:
            return jsonify({
                'ok':      False,
                'reason': f'有 {len(running)} 个 AI 后台任务正在运行，'
                         '保存配置可能会影响其结果。请等待完成或先停止任务后再保存。',
                'running_jobs': running,
                'need_force':   True,
            })

    try:
        llm_client.save_config(cfg)
    except Exception as e:
        return jsonify({'ok': False, 'reason': str(e)})

    ok = llm_client.is_configured()
    current_app.jinja_env.globals['llm_enabled'] = ok
    return jsonify({'ok': True, 'llm_enabled': ok,
                    'model':          cfg['model'],
                    'active_profile': llm_client.get_active_profile_name()})


# ══════════════════════════════════════════════════════════
# P0 — 多 profile 管理
# ══════════════════════════════════════════════════════════

@llm_bp.route('/llm/profile/add', methods=['POST'])
def profile_add():
    """新增 profile（不切换激活；除非当前没有任何 profile）。"""
    data = request.get_json(force=True) or {}
    cfg, err = _validate_profile_payload(data)
    if err:
        return jsonify({'ok': False, 'reason': err})
    if not cfg.get('name'):
        return jsonify({'ok': False, 'reason': 'profile 名不能为空'})

    err = llm_client.add_profile(cfg)
    if err:
        return jsonify({'ok': False, 'reason': err})

    current_app.jinja_env.globals['llm_enabled'] = llm_client.is_configured()
    return jsonify({'ok': True, 'active_profile': llm_client.get_active_profile_name()})


@llm_bp.route('/llm/profile/update', methods=['POST'])
def profile_update():
    """更新指定 profile（含 rename）。若更新的是激活 profile 且有运行任务，禁止。"""
    data = request.get_json(force=True) or {}
    target_name = (data.get('target_name') or '').strip()
    if not target_name:
        return jsonify({'ok': False, 'reason': '需要指定 target_name'})

    cfg, err = _validate_profile_payload(data)
    if err:
        return jsonify({'ok': False, 'reason': err})

    if target_name == llm_client.get_active_profile_name() and not data.get('force'):
        running = _running_jobs_summary()
        if running:
            return jsonify({
                'ok': False,
                'reason': f'有 {len(running)} 个 AI 后台任务正在运行，'
                         '修改激活 profile 可能影响其结果。请等待完成或先停止任务。',
                'running_jobs': running,
                'need_force':   True,
            })

    err = llm_client.update_profile(target_name, cfg)
    if err:
        return jsonify({'ok': False, 'reason': err})

    current_app.jinja_env.globals['llm_enabled'] = llm_client.is_configured()
    return jsonify({'ok': True, 'active_profile': llm_client.get_active_profile_name()})


@llm_bp.route('/llm/profile/delete', methods=['POST'])
def profile_delete():
    """删除 profile（拒绝删最后一个）。删激活时若有运行任务，禁止。"""
    data = request.get_json(force=True) or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'ok': False, 'reason': '需要 name'})

    if name == llm_client.get_active_profile_name() and not data.get('force'):
        running = _running_jobs_summary()
        if running:
            return jsonify({
                'ok': False,
                'reason': f'有 {len(running)} 个 AI 后台任务正在运行，'
                         '删除激活 profile 会切换到其他配置并影响其结果。请先停止任务。',
                'running_jobs': running,
                'need_force':   True,
            })

    err = llm_client.delete_profile(name)
    if err:
        return jsonify({'ok': False, 'reason': err})

    current_app.jinja_env.globals['llm_enabled'] = llm_client.is_configured()
    return jsonify({'ok': True, 'active_profile': llm_client.get_active_profile_name()})


@llm_bp.route('/llm/profile/activate', methods=['POST'])
def profile_activate():
    """切换激活 profile。若有运行任务，禁止（除非 force）。"""
    data = request.get_json(force=True) or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'ok': False, 'reason': '需要 name'})

    if name == llm_client.get_active_profile_name():
        return jsonify({'ok': True, 'active_profile': name, 'no_op': True})

    if not data.get('force'):
        running = _running_jobs_summary()
        if running:
            return jsonify({
                'ok': False,
                'reason': f'有 {len(running)} 个 AI 后台任务正在运行，'
                         '切换 profile 会让它们使用新配置并出现"半路换道"。'
                         '请等待完成或先停止任务。',
                'running_jobs': running,
                'need_force':   True,
            })

    err = llm_client.activate_profile(name)
    if err:
        return jsonify({'ok': False, 'reason': err})

    current_app.jinja_env.globals['llm_enabled'] = llm_client.is_configured()
    cfg = llm_client.get_config()
    return jsonify({'ok':             True,
                    'active_profile': llm_client.get_active_profile_name(),
                    'model':          cfg.get('model', '') if cfg else ''})


# ══════════════════════════════════════════════════════════
# P1 — 多条匹配智能推荐
# ══════════════════════════════════════════════════════════

@llm_bp.route('/llm/rank_entries', methods=['POST'])
def rank_entries():
    if not llm_client.is_configured():
        return jsonify({'ok': False, 'reason': 'LLM 未配置'})
    data          = request.get_json() or {}
    entries       = data.get('entries', [])
    current_error = data.get('current_error', {})
    if not entries:
        return jsonify({'ok': False, 'reason': '无候选条目'})

    N           = len(entries)
    level       = current_error.get('level', '')
    error_id    = current_error.get('error_id', '')
    location    = current_error.get('location', '')
    description = str(current_error.get('description', ''))[:500]

    cand_lines = '\n'.join(
        f'[{i}] ID:{e.get("错误ID","")} | 模块:{e.get("所属模块","")} | '
        f'原因:{str(e.get("报错原因",""))[:80]} | '
        f'关键词:{e.get("关键描述关键词","")} | 用例:{e.get("关联用例","")}'
        for i, e in enumerate(entries)
    )
    user_prompt = (
        '根据以下当前错误，对候选知识库条目按相关性从高到低排序，说明每条推荐理由。\n\n'
        f'当前错误：\n级别：{level} | 错误ID：{error_id}\n位置：{location}\n描述：{description}\n\n'
        f'候选条目（共{N}条）：\n{cand_lines}\n\n'
        '返回 JSON：\n'
        '{\n'
        '  "ranked":      [2, 0, 1, ...],\n'
        '  "reasons":     ["条目[2]推荐原因（≤30字）", ...],\n'
        '  "focus_cases": ["tc_xxx", "tc_yyy"]\n'
        '}\n'
        'focus_cases：从所有候选条目「关联用例」中挑选最相关的优先回归验证用例（去重，最多5条）。全为空则返回 []。'
    )
    messages = [
        {'role': 'system', 'content': '你是一名经验丰富的芯片验证工程师。'},
        {'role': 'user',   'content': user_prompt},
    ]
    raw = llm_client.call_llm(messages, temperature=0.2, max_tokens=600)
    if not raw:
        return jsonify({'ok': False, 'reason': 'LLM 调用失败'})

    parsed      = _parse_json_safe(raw) or {}
    ranked      = parsed.get('ranked', list(range(N)))
    reasons     = list(parsed.get('reasons', []))
    focus_cases = list(parsed.get('focus_cases', []))

    valid   = [i for i in ranked if isinstance(i, int) and 0 <= i < N]
    missing = [i for i in range(N) if i not in valid]
    valid  += missing
    reasons += [''] * N
    return jsonify({
        'ok':          True,
        'ranked':      valid,
        'reasons':     reasons[:len(valid)],
        'focus_cases': focus_cases[:5],
    })


# ══════════════════════════════════════════════════════════
# P2 — 自定义提取（路径模式单文件，多轮对话）
# ══════════════════════════════════════════════════════════

@llm_bp.route('/llm/custom_extract', methods=['POST'])
def custom_extract():
    sid  = state._sid()
    data = request.get_json() or {}

    query      = str(data.get('query') or '').strip()
    line_start = data.get('line_start')
    line_end   = data.get('line_end')
    clear      = bool(data.get('clear'))

    if clear:
        state._set_p3_history(sid, [])
        return jsonify({'ok': True, 'cleared': True})

    if not query:
        return jsonify({'ok': False, 'reason': '查询内容不能为空'})
    if not llm_client.is_configured():
        return jsonify({'ok': False, 'reason': 'LLM 未配置'})

    file_paths = state._get_file_paths(sid)
    if not file_paths:
        return jsonify({'ok': False,
                        'reason': '当前会话无可分析的文件（请先在首页上传或指定路径）'})

    # 多文件场景：用户指定 file_index；不指定默认 0（首文件，向后兼容）
    try:
        file_index = int(data.get('file_index', 0))
    except (TypeError, ValueError):
        return jsonify({'ok': False,
                        'reason': 'file_index 必须为整数'})
    if not _validate_file_index(file_index, len(file_paths)):
        return jsonify({'ok': False,
                        'reason': f'file_index {file_index} 越界（共 {len(file_paths)} 个文件）'})

    filepath  = file_paths[file_index]
    cfg       = llm_client.get_config()
    max_lines = _adaptive_max_lines(cfg)   # context_window 自适应 + 用户上限钳制

    # ── 定位片段（多块拼接） ────────────────────────────────
    coverage_warning = None
    blocks_info = []   # [{start, end, lines: [(lineno, content)]}]

    if line_start and line_end:
        ls, le    = int(line_start), int(line_end)
        raw_lines = _read_lines_range(filepath, ls, le)
        win_start, win_end = ls, le
        if len(raw_lines) > max_lines:
            raw_lines = raw_lines[:max_lines]
            win_end   = raw_lines[-1][0] if raw_lines else le
            coverage_warning = (
                f'指定范围共 {le - ls + 1} 行，超出最大提取行数 {max_lines}，'
                f'已截取第 {ls}~{win_end} 行。'
            )
        blocks_info = [{'start': win_start, 'end': win_end, 'lines': raw_lines}]
    else:
        # C: 多块预扫描
        blocks, fa, la, _total = _p3_prescan_blocks(
            filepath, query, state.EXTRA_PATTERNS, max_lines)
        for bs, be, _bw in blocks:
            blines = _read_lines_range(filepath, bs, be)
            if blines:
                blocks_info.append({
                    'start': blines[0][0], 'end': blines[-1][0], 'lines': blines
                })
        # 拍平所有块到 raw_lines（兼容下游 _apply_token_budget / _format_log_lines）
        raw_lines = [pair for b in blocks_info for pair in b['lines']]
        win_start = blocks_info[0]['start'] if blocks_info else 1
        win_end   = blocks_info[-1]['end']  if blocks_info else 1
        # F: 用"窗外覆盖率"替代首末跨度——分散簇不再误报
        total_lines_in_blocks = sum(len(b['lines']) for b in blocks_info)
        total_span = (la - fa + 1) if (fa and la) else 0
        if total_span > max_lines and fa and la and len(blocks_info) <= 1:
            window_span = win_end - win_start + 1
            outside_estimate = max(0, total_span - window_span)
            if outside_estimate > max_lines // 2:
                next_end = min(fa + max_lines - 1, la)
                coverage_warning = (
                    f'相关内容分布在第 {fa}~{la} 行（跨度 {total_span} 行），'
                    f'已展示密度最高的第 {win_start}~{win_end} 行（共 {len(raw_lines)} 行）。'
                    f'如需查看其他位置，请在查询中指定行号范围：「行号 {fa}-{next_end}」。'
                )
        elif len(blocks_info) > 1:
            # 多块情况：明确告知用户已展示几个段
            seg_summary = ' / '.join(
                f"L{b['start']}-L{b['end']}" for b in blocks_info)
            coverage_warning = (
                f'已展示 {len(blocks_info)} 个相关段（{seg_summary}），'
                f'共 {total_lines_in_blocks} 行。如需调整请用行号范围。'
            )

    # ── Token 预算安全检查 ────────────────────────────────
    raw_lines, win_start, win_end, budget_trimmed = _apply_token_budget(
        raw_lines, cfg, win_start, win_end)
    if budget_trimmed:
        trim_msg = (
            f'内容已因 token 预算限制（context_window={cfg.get("context_window")}）'
            f'收缩至第 {win_start}~{win_end} 行。'
        )
        coverage_warning = f'{coverage_warning} {trim_msg}' if coverage_warning else trim_msg

    # 多块情况：按段添加分隔头让 LLM 知道是不连续的多段
    if len(blocks_info) > 1:
        # 重新按行号 partition raw_lines（_apply_token_budget 可能裁过端点）
        blocks_render = []
        cur = []
        cur_block_idx = 0
        block_ranges = [(b['start'], b['end']) for b in blocks_info]
        for ln, content in raw_lines:
            # 找该 ln 属于哪个块
            while cur_block_idx < len(block_ranges) and ln > block_ranges[cur_block_idx][1]:
                if cur:
                    blocks_render.append((block_ranges[cur_block_idx][0],
                                          block_ranges[cur_block_idx][1], cur))
                    cur = []
                cur_block_idx += 1
            if cur_block_idx < len(block_ranges):
                cur.append((ln, content))
        if cur and cur_block_idx < len(block_ranges):
            blocks_render.append((block_ranges[cur_block_idx][0],
                                  block_ranges[cur_block_idx][1], cur))
        # 渲染：每段加 "[段 N: L<start>-L<end>]" 头
        sections = []
        for i, (bs, be, lines) in enumerate(blocks_render, 1):
            actual_s = lines[0][0] if lines else bs
            actual_e = lines[-1][0] if lines else be
            sections.append(
                f'[段 {i}: L{actual_s}-L{actual_e}]\n' + _format_log_lines(lines))
        log_content = '\n\n'.join(sections)
    else:
        log_content = _format_log_lines(raw_lines)
    filename    = Path(filepath).name
    N           = len(raw_lines)

    # ── 多轮历史管理 ──────────────────────────────────────
    p3_history = state._get_p3_history(sid)

    # 判断是否需要重新提取：当前历史首条中找不到本次查询关键词
    need_rescan = False
    if p3_history:
        kws          = _extract_query_keywords(query)
        first_content = p3_history[0].get('content', '').lower()
        if kws and not any(kw.lower() in first_content for kw in kws):
            need_rescan = True

    format_type, format_instr = _get_format_instruction(query)
    coverage_note = f'\n⚠️ {coverage_warning}\n' if coverage_warning else ''

    if not p3_history or need_rescan:
        user_content = (
            f'以下是日志文件「{filename}」的部分内容，\n'
            f'提取范围：第 {win_start} 行 ～ 第 {win_end} 行，共 {N} 行。'
            f'{coverage_note}\n\n'
            f'<日志内容>\n{log_content}\n</日志内容>\n\n'
            f'查询：{query}\n\n{format_instr}'
        )
        messages = [
            {'role': 'system', 'content': _P3_SYSTEM_PROMPT},
            {'role': 'user',   'content': user_content},
        ]
    else:
        user_content = f'{query}\n\n{format_instr}'
        messages = (
            [{'role': 'system', 'content': _P3_SYSTEM_PROMPT}]
            + p3_history
            + [{'role': 'user', 'content': user_content}]
        )

    # ── 调用 LLM ─────────────────────────────────────────
    result_text = llm_client.call_llm(messages, temperature=0.1, max_tokens=2000)
    if not result_text:
        return jsonify({'ok': False, 'reason': 'LLM 调用失败或超时，请稍后重试'})

    # ── 解析返回格式 ──────────────────────────────────────
    detected_format = format_type
    parsed_data     = result_text
    if format_type == 'json':
        arr = _parse_json_safe(result_text, array=True)
        if arr is not None:
            parsed_data     = arr
            detected_format = 'json'
        else:
            detected_format = 'text'

    # ── 更新历史（极端情况裁剪：保留首 2 条 + 最后 10 条）──
    if not p3_history or need_rescan:
        new_history = [{'role': 'user', 'content': user_content}]
    else:
        new_history = list(p3_history) + [{'role': 'user', 'content': user_content}]
    new_history.append({'role': 'assistant', 'content': result_text})
    if len(new_history) > 12:
        new_history = new_history[:2] + new_history[-10:]
    state._set_p3_history(sid, new_history)

    turns = sum(1 for m in new_history if m['role'] == 'assistant')
    # 构造 blocks 字段（多块）—— 单块时 blocks 是 1 元素列表，前端可统一处理
    response_blocks = []
    if len(blocks_info) > 1:
        # 重新按行号 partition raw_lines（_apply_token_budget 可能裁过端点）
        block_ranges = [(b['start'], b['end']) for b in blocks_info]
        cur_idx = 0
        bucket = []
        for ln, content in raw_lines:
            while cur_idx < len(block_ranges) and ln > block_ranges[cur_idx][1]:
                if bucket:
                    response_blocks.append({
                        'start':  bucket[0][0],
                        'end':    bucket[-1][0],
                        'lines': [{'lineno': ln_, 'content': c_}
                                  for ln_, c_ in bucket],
                    })
                    bucket = []
                cur_idx += 1
            if cur_idx < len(block_ranges):
                bucket.append((ln, content))
        if bucket and cur_idx < len(block_ranges):
            response_blocks.append({
                'start':  bucket[0][0],
                'end':    bucket[-1][0],
                'lines': [{'lineno': ln_, 'content': c_} for ln_, c_ in bucket],
            })
    else:
        response_blocks = [{
            'start':  win_start,
            'end':    win_end,
            'lines': [{'lineno': ln, 'content': content} for ln, content in raw_lines],
        }]

    return jsonify({
        'ok':               True,
        'format':           detected_format,
        'data':             parsed_data,
        'extracted_start':  win_start,
        'extracted_end':    win_end,
        'total_lines_sent': N,
        'turns':            turns,
        'coverage_warning': coverage_warning,
        'raw_lines': [{'lineno': ln, 'content': content} for ln, content in raw_lines],
        'blocks':           response_blocks,
    })


# ══════════════════════════════════════════════════════════
# P3 — 相似错误推荐（写回辅助）
# ══════════════════════════════════════════════════════════

@llm_bp.route('/llm/similar_errors', methods=['POST'])
def similar_errors():
    if not llm_client.is_configured():
        return jsonify({'ok': False, 'similar': []})
    data = request.get_json() or {}
    try:
        db_path = state._validate_db_path(data.get('db_path', ''))
    except ValueError as e:
        return jsonify({'ok': False, 'similar': [], 'reason': str(e)}), 400

    level       = str(data.get('level', ''))
    error_id    = str(data.get('error_id', ''))
    description = str(data.get('description', ''))[:500]
    top_k       = int(data.get('top_k', 5))

    try:
        db_entries = load_db(db_path)
    except Exception as e:
        return jsonify({'ok': False, 'similar': [], 'reason': str(e)}), 500

    # 预筛 top-50（token 重叠，不调 LLM）
    scored     = score_query(db_entries, description, level)[:50]
    candidates = [e for _, e in scored]
    if not candidates:
        return jsonify({'ok': True, 'similar': []})

    cand_lines = '\n'.join(
        f'[{i}] ID:{e.get("错误ID","")} | '
        f'原因:{str(e.get("报错原因",""))[:80]} | '
        f'方案:{str(e.get("解决方案",""))[:60]} | '
        f'关键词:{e.get("关键描述关键词","")}'
        for i, e in enumerate(candidates)
    )
    user_prompt = (
        f'以下是一个尚未匹配的 UVM 错误：\n'
        f'级别：{level} | 错误ID：{error_id}\n描述：{description}\n\n'
        f'以下是知识库候选条目（共{len(candidates)}条，已按关键词相关度预筛选）：\n'
        f'{cand_lines}\n\n'
        '请找出与当前错误根因相同或高度相似的条目（最多5条），无则返回空列表。\n'
        '返回 JSON：{"similar": [{"idx": 0, "reason": "≤30字相似原因"}]}'
    )
    messages = [
        {'role': 'system', 'content': '你是一名经验丰富的芯片验证工程师。'},
        {'role': 'user',   'content': user_prompt},
    ]
    raw = llm_client.call_llm_with_cache(messages, temperature=0.2, max_tokens=400)
    if not raw:
        return jsonify({'ok': True, 'similar': []})

    parsed = _parse_json_safe(raw) or {}
    result = []
    for item in (parsed.get('similar') or [])[:top_k]:
        idx = item.get('idx')
        if not isinstance(idx, int) or idx < 0 or idx >= len(candidates):
            continue
        entry = candidates[idx]
        result.append({
            '_row_idx':          entry.get('_row_idx'),
            '错误ID':            entry.get('错误ID', ''),
            '报错原因':           entry.get('报错原因', ''),
            '解决方案':           entry.get('解决方案', ''),
            '关键描述关键词':     entry.get('关键描述关键词', ''),
            'similarity_reason': str(item.get('reason', '')),
        })
    return jsonify({'ok': True, 'similar': result})


# ══════════════════════════════════════════════════════════
# P4 — 批量错误模式分析
# ══════════════════════════════════════════════════════════

@llm_bp.route('/llm/batch_patterns', methods=['POST'])
def batch_patterns():
    if not llm_client.is_configured():
        return jsonify({'ok': False, 'reason': 'LLM 未配置'})
    sid     = state._sid()
    results, _ = state._get_results(sid)
    if not results:
        return jsonify({'ok': False, 'reason': '无分析结果'})

    # 按 (level, error_id) 去重统计
    seen = {}
    for r in results:
        for err in r.get('top_errors', []):
            key = (err.get('level', ''), err.get('error_id', ''))
            if key not in seen:
                seen[key] = {'file_count': 0, 'description': err.get('description', '')}
            seen[key]['file_count'] += 1

    if not seen:
        return jsonify({'ok': False, 'reason': '无错误数据'})

    top20 = sorted(seen.items(), key=lambda x: -x[1]['file_count'])[:20]
    err_lines = '\n'.join(
        f'[{i}] 级别:{level} | ID:{eid} | '
        f'出现{info["file_count"]}个文件 | 描述:{info["description"][:150]}'
        for i, ((level, eid), info) in enumerate(top20, 1)
    )
    user_prompt = (
        '以下是一批回归测试的失败错误统计（已去重，按出现文件数降序）：\n\n'
        f'{err_lines}\n\n'
        '请归纳 3~7 个主要失败模式，返回 JSON：\n'
        '{\n'
        '  "patterns": [\n'
        '    {\n'
        '      "title":            "一句话模式标题",\n'
        '      "error_ids":        ["ID1", "ID2"],\n'
        '      "file_count":       15,\n'
        '      "description":      "模式特征说明",\n'
        '      "suggested_action": "建议排查方向"\n'
        '    }\n'
        '  ]\n'
        '}'
    )
    messages = [
        {'role': 'system', 'content': '你是一名日志分析专家，擅长归纳批量测试失败的根本原因模式。请严格以 JSON 格式回答。'},
        {'role': 'user',   'content': user_prompt},
    ]
    raw = llm_client.call_llm(messages, temperature=0.3, max_tokens=800)
    if not raw:
        return jsonify({'ok': False, 'reason': 'LLM 调用失败'})

    parsed   = _parse_json_safe(raw) or {}
    patterns = parsed.get('patterns', [])
    return jsonify({'ok': True, 'total_files': len(results), 'patterns': patterns})


# ══════════════════════════════════════════════════════════
# P5 — 语义知识库查询增强
# ══════════════════════════════════════════════════════════

@llm_bp.route('/llm/semantic_query', methods=['POST'])
def semantic_query():
    if not llm_client.is_configured():
        return jsonify({'ok': False})
    data       = request.get_json() or {}
    text       = str(data.get('text', ''))
    candidates = data.get('candidates', [])
    if not candidates:
        return jsonify({'ok': False, 'reason': '无候选条目'})

    N = len(candidates)
    cand_lines = '\n'.join(
        f'[{i}] ID:{e.get("错误ID","")} | 级别:{e.get("错误类型","")} | '
        f'原因:{str(e.get("报错原因",""))[:80]} | 关键词:{e.get("关键描述关键词","")}'
        for i, e in enumerate(candidates)
    )
    user_prompt = (
        f'用户查询：{text}\n\n'
        f'候选条目（共{N}条）：\n{cand_lines}\n\n'
        '任务：从候选条目中找出与用户查询语义相关的条目，按相关性从高到低排列。\n'
        '规则：\n'
        '- 只包含真正相关的条目，完全不相关的不要放入结果\n'
        '- 如果所有条目都与查询无关，返回空数组 ranked:[]\n'
        '- reasons 长度与 ranked 相同，每条 ≤20 字说明相关原因\n'
        '仅返回 JSON，格式：{"ranked":[2,0,1], "reasons":["原因","原因","原因"]}'
    )
    messages = [
        {'role': 'system', 'content': (
            '你是一名知识库搜索专家，负责语义相关性筛选。'
            '必须严格判断相关性：只有内容与用户查询直接相关才纳入结果，'
            '宽泛、间接、仅含公共词汇的条目应排除。'
            '仅返回 JSON，不含其他文字。'
        )},
        {'role': 'user', 'content': user_prompt},
    ]
    raw = llm_client.call_llm_with_cache(messages, temperature=0, max_tokens=400)
    if not raw:
        return jsonify({'ok': False})

    parsed  = _parse_json_safe(raw) or {}
    ranked  = list(parsed.get('ranked', []))
    reasons = list(parsed.get('reasons', []))

    # 只保留合法 index，不补全不相关条目
    valid = [i for i in ranked if isinstance(i, int) and 0 <= i < N]
    return jsonify({'ok': True, 'ranked': valid, 'reasons': reasons[:len(valid)]})


# ══════════════════════════════════════════════════════════
# P6 — 知识库语义去重质量检查
# ══════════════════════════════════════════════════════════

def _run_review_job(job_id: str, db_path: str, mode: str, cfg: dict):
    """后台线程：执行知识库质量检查，逐组调用 LLM。"""

    def _upd(patch: dict):
        with _review_lock:
            if job_id in _review_jobs:
                _review_jobs[job_id].update(patch)
                _review_jobs[job_id]['ts'] = time.time()

    def _stopped():
        with _review_lock:
            return _review_jobs.get(job_id, {}).get('stop', False)

    try:
        db_entries = load_db(db_path)
    except Exception as e:
        _upd({'status': 'error', 'reason': str(e)})
        return

    window_size = int(cfg.get('kb_review_window_size', 20))
    step_size   = int(cfg.get('kb_review_step_size',   10))
    batch_size  = int(cfg.get('kb_review_batch_size',  50))

    # 按错误类型分组，过滤单条组
    groups = {}
    for e in db_entries:
        lvl = str(e.get('错误类型', '')).strip().upper() or 'UNKNOWN'
        groups.setdefault(lvl, []).append(e)
    all_levels   = [lvl for lvl in groups if len(groups[lvl]) >= 2]
    total_groups = len(all_levels)

    suspect_pairs   = []
    skipped_batches = 0
    seen_pairs      = set()

    _upd({'status': 'running', 'total': total_groups, 'done': 0,
          'suspect_pairs': [], 'skipped': 0, 'group': ''})

    for g_idx, level in enumerate(all_levels):
        if _stopped():
            break
        _upd({'group': level, 'done': g_idx})

        entries_g = groups[level]
        N = len(entries_g)

        if mode == 'fast':
            batches = [entries_g[p: p + window_size]
                       for p in range(0, N, step_size)]
        else:
            batches = [entries_g[p: p + batch_size]
                       for p in range(0, N, batch_size)]

        for batch in batches:
            if _stopped():
                break
            M = len(batch)
            batch_lines = '\n'.join(
                f'[{i}] row:{e.get("_row_idx","")} | ID:{e.get("错误ID","")} | '
                f'原因:{str(e.get("报错原因",""))[:80]} | '
                f'方案:{str(e.get("解决方案",""))[:60]}'
                for i, e in enumerate(batch)
            )
            user_prompt = (
                f'错误类型「{level}」的知识库条目（共{M}条）：\n\n'
                f'{batch_lines}\n\n'
                '返回：{"pairs": [{"a":0, "b":3, "reason":"≤30字相似原因"}]}\n'
                '（a/b 为列表索引，不是 row 号）'
            )
            messages = [
                {'role': 'system', 'content': '你是一名知识库维护专家。请找出以下条目中描述同一根因的重复对。仅返回 JSON，无重复时返回 {"pairs": []}。'},
                {'role': 'user',   'content': user_prompt},
            ]
            raw = llm_client.call_llm_with_cache(messages, temperature=0.1, max_tokens=500)
            if not raw:
                skipped_batches += 1
                continue

            parsed = _parse_json_safe(raw) or {}
            for pair in (parsed.get('pairs') or []):
                a, b = pair.get('a'), pair.get('b')
                if not (isinstance(a, int) and isinstance(b, int)):
                    continue
                if a < 0 or a >= M or b < 0 or b >= M or a == b:
                    continue
                ea, eb   = batch[a], batch[b]
                ra, rb   = ea.get('_row_idx'), eb.get('_row_idx')
                pair_key = frozenset({ra, rb})
                if pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)
                suspect_pairs.append({
                    'row_a': dict(ea, _row_idx=ra),
                    'row_b': dict(eb, _row_idx=rb),
                    'similarity_reason': str(pair.get('reason', '')),
                })
            _upd({'suspect_pairs': suspect_pairs, 'skipped': skipped_batches})

    _upd({'status': 'done', 'done': total_groups,
          'suspect_pairs': suspect_pairs, 'skipped': skipped_batches})


@llm_bp.route('/llm/merge_suggest', methods=['POST'])
def merge_suggest():
    """P6 合并建议：给定两条疑似重复条目，返回 AI 建议合并后的字段值。"""
    data = request.get_json() or {}
    ra = data.get('row_a', {})
    rb = data.get('row_b', {})
    if not ra or not rb:
        return jsonify({'ok': False, 'reason': '缺少条目数据'})
    if not llm_client.is_configured():
        return jsonify({'ok': False, 'reason': 'LLM 未配置'})

    FIELDS = ['错误类型', '错误ID', '关键描述关键词', '报错原因',
              '所属模块', '根因分类', '解决方案', '关联用例', '录入人']
    a_text = '\n'.join(f'{f}: {ra.get(f, "")}' for f in FIELDS)
    b_text = '\n'.join(f'{f}: {rb.get(f, "")}' for f in FIELDS)
    prompt = (
        f'以下是两条疑似重复的知识库条目，请合并为一条最完整的记录。\n\n'
        f'条目A：\n{a_text}\n\n条目B：\n{b_text}\n\n'
        '合并规则：\n'
        '- 关键描述关键词：合并两者（去重，逗号分隔）\n'
        '- 关联用例：合并两者（去重，逗号分隔）\n'
        '- 报错原因/解决方案：选更详细的一条或融合补充\n'
        '- 其余字段：非空优先，A优先于B\n'
        f'仅返回 JSON，格式：{{"错误类型":"...","错误ID":"...",...（共{len(FIELDS)}个字段）}}'
    )
    messages = [
        {'role': 'system', 'content': '你是知识库管理专家，只返回合并后的 JSON，不含其他文字。'},
        {'role': 'user',   'content': prompt},
    ]
    raw = llm_client.call_llm(messages, temperature=0, max_tokens=600)
    merged = _parse_json_safe(raw) or {}
    if not merged:
        merged = {f: ra.get(f) or rb.get(f) or '' for f in FIELDS}
    return jsonify({'ok': True, 'merged': merged})


@llm_bp.route('/llm/kb_review', methods=['POST'])
def kb_review():
    if not llm_client.is_configured():
        return jsonify({'ok': False, 'reason': 'LLM 未配置'})
    data = request.get_json() or {}
    try:
        db_path = state._validate_db_path(data.get('db_path', ''))
    except ValueError as e:
        return jsonify({'ok': False, 'reason': str(e)}), 400

    mode = data.get('mode', 'fast')
    cfg  = llm_client.get_config() or {}

    _cleanup_review_jobs()
    job_id = str(uuid.uuid4())
    creator_sid = state._sid()    # M-3: 绑定创建者 sid 用于 export/stop 鉴权
    with _review_lock:
        _review_jobs[job_id] = {
            'status': 'pending', 'group': '', 'done': 0, 'total': 0,
            'suspect_pairs': [], 'skipped': 0, 'stop': False,
            'db_path': db_path, 'ts': time.time(),
            'creator_sid': creator_sid,
        }

    threading.Thread(
        target=_run_review_job,
        args=(job_id, db_path, mode, cfg),
        daemon=True,
    ).start()
    return jsonify({'ok': True, 'job_id': job_id})


@llm_bp.route('/llm/kb_review_status', methods=['GET'])
def kb_review_status():
    job_id = request.args.get('job_id', '')
    with _review_lock:
        job = dict(_review_jobs.get(job_id, {}))
    if not job:
        return jsonify({'status': 'error', 'reason': '任务不存在或已过期'})
    # M-3: 仅创建者可查 status（避免 job_id 泄漏后他人窃取进度）
    if job.get('creator_sid') and job['creator_sid'] != state._sid():
        return jsonify({'status': 'error', 'reason': '无权访问此任务'})

    status = job.get('status', 'pending')
    if status == 'running':
        done  = job.get('done', 0)
        total = max(job.get('total', 1), 1)
        elapsed = time.time() - job.get('ts', time.time())
        eta_min = round((total - done) * (elapsed / max(done, 1)) / 60, 1) if done else None
        return jsonify({'status': 'running', 'group': job.get('group', ''),
                        'done': done, 'total': total, 'eta_min': eta_min,
                        'found': len(job.get('suspect_pairs', []))})
    if status == 'done':
        return jsonify({'status': 'done',
                        'suspect_pairs': job.get('suspect_pairs', []),
                        'skipped': job.get('skipped', 0)})
    if status == 'error':
        return jsonify({'status': 'error', 'reason': job.get('reason', '')})
    return jsonify({'status': status})


@llm_bp.route('/llm/kb_review_stop', methods=['POST'])
def kb_review_stop():
    job_id = (request.get_json() or {}).get('job_id', '')
    sid_now = state._sid()
    with _review_lock:
        job = _review_jobs.get(job_id)
        if not job:
            return jsonify({'ok': False, 'reason': '任务不存在或已过期'})
        # M-3: 仅创建者可停止
        if job.get('creator_sid') and job['creator_sid'] != sid_now:
            return jsonify({'ok': False, 'reason': '无权停止此任务'}), 403
        job['stop'] = True
    return jsonify({'ok': True})


@llm_bp.route('/llm/kb_review_export', methods=['GET'])
def kb_review_export():
    job_id = request.args.get('job_id', '')
    with _review_lock:
        job = dict(_review_jobs.get(job_id, {}))
    if not job or job.get('status') not in ('done', 'running'):
        return jsonify({'error': '任务不存在或尚未完成'}), 404
    # M-3: 仅任务创建者可下载
    if job.get('creator_sid') and job['creator_sid'] != state._sid():
        return jsonify({'error': '无权访问此任务的导出结果'}), 403

    pairs = job.get('suspect_pairs', [])
    if not pairs:
        return jsonify({'error': '无疑似重复对'}), 404

    try:
        import openpyxl
    except ImportError:
        return jsonify({'error': 'openpyxl 未安装'}), 500

    wb  = openpyxl.Workbook()
    ws1 = wb.active
    ws1.title = '疑似重复对'
    ws1.append(['序号', '行号A', '错误类型A', '错误ID-A', '报错原因摘要A',
                '行号B', '错误类型B', '错误ID-B', '报错原因摘要B', '相似原因'])
    for i, p in enumerate(pairs, 1):
        ra, rb = p['row_a'], p['row_b']
        ws1.append([i,
                    ra.get('_row_idx', ''), ra.get('错误类型', ''), ra.get('错误ID', ''),
                    str(ra.get('报错原因', ''))[:80],
                    rb.get('_row_idx', ''), rb.get('错误类型', ''), rb.get('错误ID', ''),
                    str(rb.get('报错原因', ''))[:80],
                    p.get('similarity_reason', '')])

    _KB_COLS = ['错误类型', '错误ID', '关键描述关键词', '报错原因',
                '所属模块', '根因分类', '解决方案', '关联用例', '录入人', '录入日期']
    db_entries_map = {}
    try:
        db_path = job.get('db_path', '')
        if db_path:
            for e in load_db(db_path):
                db_entries_map[e.get('_row_idx')] = e
    except Exception:
        pass

    for sheet_name, row_key in [('条目A详情', 'row_a'), ('条目B详情', 'row_b')]:
        ws = wb.create_sheet(sheet_name)
        ws.append(['序号', '_row_idx'] + _KB_COLS)
        for i, p in enumerate(pairs, 1):
            ri    = p[row_key].get('_row_idx')
            entry = db_entries_map.get(ri, p[row_key])
            ws.append([i, ri] + [str(entry.get(c, '')) for c in _KB_COLS])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                     as_attachment=True,
                     download_name=f'kb_review_{job_id[:8]}.xlsx')
