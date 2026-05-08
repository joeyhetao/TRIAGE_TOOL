# -*- coding: utf-8 -*-
"""
LLM 功能 Blueprint — 所有 AI 辅助路由。

路由列表（共 14 条）：
  POST /llm/reload_config      P0: 热重载配置
  GET  /llm/get_config         P0: 获取当前配置（api_key 脱敏）
  POST /llm/save_config        P0: 保存配置到 llm_config.json
  POST /llm/test_connection    P0: 连接测试
  POST /llm/rank_entries       P1: 多条匹配智能推荐
  POST /llm/custom_extract     P2: 自定义提取（路径模式单文件，多轮对话）
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


def _extract_query_keywords(query: str) -> list:
    """
    从查询文本中提取关键词，用于预扫描锚定。
    支持中文混合查询：按中文字符、数字、空白符分割后再提取英文/符号 token。
    """
    kws = []
    kws += re.findall(r'"([^"]+)"', query)                       # 引号内短语
    kws += re.findall(r'\b[A-Z][A-Z0-9_]{2,}\b', query)         # 形似错误ID（大写）
    stripped = re.sub(r'"[^"]*"', ' ', query)
    stripped = re.sub(r'\b[A-Z][A-Z0-9_]{2,}\b', ' ', stripped)
    # 按中文字符 + 数字 + 空白切分，提取英文/符号 token（如 uvm_error）
    for w in re.split(r'[\s\d\u4e00-\u9fff\uff00-\uffef]+', stripped):
        w = w.strip('_- ')
        if len(w) > 2:
            kws.append(w)
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
    r'前\s*\d|第\s*[一1]|^first|^top\s*\d|earliest|最早|最前', re.IGNORECASE)


def _query_prefers_start(query: str) -> bool:
    """判断查询是否意图获取文件开头的内容（前N个、第一个等）。"""
    return bool(_PREFER_START_PAT.search(query))


def _p3_prescan(filepath: str, query: str, extra_patterns: list,
                max_lines: int) -> tuple:
    """
    单次流式扫描文件，用滑动窗口找密度最高的连续 max_lines 行区间。
    若查询含"前N个/第一/first/top"等意图，优先从首个锚点开始。
    返回 (win_start, win_end, total_span, first_anchor, last_anchor, total_lines)
    所有行号均为 1-based。
    """
    keywords   = [kw.lower() for kw in _extract_query_keywords(query)]
    extra_lower = [p.lower() for p in extra_patterns]

    uvm_nos = []
    kw_nos  = []
    total_lines = 0

    try:
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            for lineno, line in enumerate(f, 1):
                total_lines = lineno
                ll = line.lower()
                if _UVM_REAL_PAT.search(line) or any(p in ll for p in extra_lower):
                    uvm_nos.append(lineno)
                if keywords and any(kw in ll for kw in keywords):
                    kw_nos.append(lineno)
    except Exception:
        return 1, min(max_lines, 1), 0, 0, 0, 1

    anchor_nos = kw_nos if kw_nos else uvm_nos

    if not anchor_nos:
        end = min(max_lines, total_lines)
        return 1, end, 0, 0, 0, total_lines

    first_anchor = min(anchor_nos)
    last_anchor  = max(anchor_nos)
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

    # 滑动窗口：找包含最多锚点的连续 max_lines 行
    sorted_a  = sorted(anchor_nos)
    best_count = 0
    best_win_s = sorted_a[0]
    left = 0
    for i, ra in enumerate(sorted_a):
        win_s = ra - max_lines + 1
        while left < len(sorted_a) and sorted_a[left] < win_s:
            left += 1
        count = i - left + 1
        if count > best_count:
            best_count = count
            best_win_s = max(1, win_s)

    win_start = best_win_s
    win_end   = min(total_lines, win_start + max_lines - 1)
    win_start = max(1, win_end - max_lines + 1)
    return win_start, win_end, total_span, first_anchor, last_anchor, total_lines


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


def _parse_json_safe(text: str, array: bool = False):
    """从 LLM 返回文本中提取第一个 JSON 对象或数组，解析失败返回 None。"""
    pattern = r'\[.*\]' if array else r'\{.*\}'
    m = re.search(pattern, text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group())
    except Exception:
        return None


def _cleanup_review_jobs():
    with _review_lock:
        now   = time.time()
        stale = [k for k, v in list(_review_jobs.items())
                 if now - v.get('ts', 0) > _REVIEW_JOB_TTL]
        for k in stale:
            del _review_jobs[k]


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
# P0 — 获取当前配置（api_key 脱敏）
# ══════════════════════════════════════════════════════════

@llm_bp.route('/llm/get_config', methods=['GET'])
def get_config():
    cfg = llm_client.get_config()
    configured = cfg is not None
    if not configured:
        return jsonify({'ok': True, 'configured': False, 'config': {}})
    # api_key 脱敏：保留前4位 + ***
    raw_key = cfg.get('api_key', '')
    if raw_key and len(raw_key) > 4:
        masked_key = raw_key[:4] + '***'
    elif raw_key:
        masked_key = '***'
    else:
        masked_key = ''
    return jsonify({
        'ok': True,
        'configured': True,
        'config': {
            'endpoint':       cfg.get('endpoint', ''),
            'api_key':        masked_key,
            'model':          cfg.get('model', ''),
            'timeout':        cfg.get('timeout', 30),
            'context_window': cfg.get('context_window', 100000),
            'p3_max_lines':   cfg.get('p3_max_lines', 2500),
        }
    })


# ══════════════════════════════════════════════════════════
# P0 — 保存配置到 llm_config.json
# ══════════════════════════════════════════════════════════

@llm_bp.route('/llm/save_config', methods=['POST'])
def save_config():
    data = request.get_json(force=True) or {}
    endpoint = (data.get('endpoint') or '').strip()
    model    = (data.get('model') or '').strip()
    if not endpoint:
        return jsonify({'ok': False, 'reason': 'endpoint 不能为空'})
    if not endpoint.startswith('http'):
        return jsonify({'ok': False, 'reason': 'endpoint 必须以 http 开头'})
    if not model:
        return jsonify({'ok': False, 'reason': '模型名称不能为空'})

    cfg = {
        'endpoint':       endpoint,
        'api_key':        (data.get('api_key') or '').strip(),
        'model':          model,
        'timeout':        int(data.get('timeout', 30)),
        'context_window': int(data.get('context_window', 100000)),
        'p3_max_lines':   int(data.get('p3_max_lines', 2500)),
    }
    try:
        llm_client.save_config(cfg)
    except Exception as e:
        return jsonify({'ok': False, 'reason': str(e)})

    ok = llm_client.is_configured()
    current_app.jinja_env.globals['llm_enabled'] = ok
    return jsonify({'ok': True, 'llm_enabled': ok, 'model': model})


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
                        'reason': '当前会话无文件路径（仅路径模式单文件分析支持自定义提取）'})
    filepath  = file_paths[0]
    cfg       = llm_client.get_config()
    max_lines = int(cfg.get('p3_max_lines', 2500))

    # ── 定位片段 ──────────────────────────────────────────
    coverage_warning = None

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
    else:
        win_start, win_end, total_span, fa, la, _ = _p3_prescan(
            filepath, query, state.EXTRA_PATTERNS, max_lines)
        raw_lines = _read_lines_range(filepath, win_start, win_end)
        if total_span > max_lines and total_span > 0:
            next_end = min(fa + max_lines - 1, la)
            coverage_warning = (
                f'相关内容跨越 {total_span} 行（第 {fa}~{la} 行），'
                f'已截取密度最高的 {len(raw_lines)} 行（第 {win_start}~{win_end} 行）。'
                f'如需查看其他部分，请在查询中指定行号范围：「行号 {fa}-{next_end}」。'
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
    with _review_lock:
        _review_jobs[job_id] = {
            'status': 'pending', 'group': '', 'done': 0, 'total': 0,
            'suspect_pairs': [], 'skipped': 0, 'stop': False,
            'db_path': db_path, 'ts': time.time(),
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
    with _review_lock:
        if job_id in _review_jobs:
            _review_jobs[job_id]['stop'] = True
    return jsonify({'ok': True})


@llm_bp.route('/llm/kb_review_export', methods=['GET'])
def kb_review_export():
    job_id = request.args.get('job_id', '')
    with _review_lock:
        job = dict(_review_jobs.get(job_id, {}))
    if not job or job.get('status') not in ('done', 'running'):
        return jsonify({'error': '任务不存在或尚未完成'}), 404

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
