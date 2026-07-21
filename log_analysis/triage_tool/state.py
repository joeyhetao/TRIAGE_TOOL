# -*- coding: utf-8 -*-
"""
共享应用状态 — 所有 Blueprint 通过 `import state` 访问此模块。

注意：EXTRA_PATTERNS / PASS_PATTERNS 是可变列表，必须通过 state.EXTRA_PATTERNS
访问，禁止 `from state import EXTRA_PATTERNS`（from-import 会持有副本，
导致其他模块的修改不可见）。
"""
import os
import sys
import json
import time
import uuid
import threading
import getpass
import re
from pathlib import Path
from flask import session

# ── 路径初始化 ────────────────────────────────────────────
if getattr(sys, 'frozen', False):
    BASE_DIR    = Path(sys.executable).parent
    _BUNDLE_DIR = Path(sys._MEIPASS)
else:
    BASE_DIR    = Path(__file__).parent
    _BUNDLE_DIR = BASE_DIR

try:
    OS_USERNAME = getpass.getuser()
except Exception:
    OS_USERNAME = ''

UPLOAD_DIR = BASE_DIR / 'uploads'
REPORT_DIR = BASE_DIR / 'reports'
DB_DEFAULT = str(BASE_DIR / 'error_db.xlsx')

UPLOAD_DIR.mkdir(exist_ok=True)
REPORT_DIR.mkdir(exist_ok=True)

MAX_FILE_SIZE  = 10 * 1024 * 1024 * 1024   # 10 GB 单文件上限
MAX_PATH_FILES = 5000                        # 路径模式单次最多文件数
TOP_N          = 5                           # 每个日志最多提取的错误条数
MAX_LEN        = 500                         # 表单字段最大字符数

from core.db_manager import BACKUP_COUNT     # 滚动备份份数（供路由层使用）

# ── 会话存储 ──────────────────────────────────────────────
_STORE_TTL  = 48 * 3600                      # expire after 48 hours
_store: dict = {}                            # sid -> {'results', 'db_path', 'file_paths', 'p3_history', 'p3_tokens', 'ts'}
_store_lock  = threading.Lock()

# ── 后台任务状态 ──────────────────────────────────────────
_JOBS_TTL  = 3600                            # 任务状态保留 1 小时
_jobs: dict = {}                             # job_id -> 任务字段
_jobs_lock  = threading.Lock()


def _cleanup_jobs():
    """清理过期的任务记录（由 /analyze 路由在创建新任务前调用）。"""
    with _jobs_lock:
        now   = time.time()
        stale = [k for k, v in list(_jobs.items()) if now - v.get('ts', 0) > _JOBS_TTL]
        for k in stale:
            del _jobs[k]

# ── 删除撤销缓冲（独立于分析会话，按 sid 分组，仅限当次进程生命周期） ──
_undo_buffers: dict = {}        # sid -> [{entry, db_path, ts}, ...]
_undo_lock    = threading.Lock()
_UNDO_MAX     = 10              # 每个 session 最多保留 10 条可撤销删除


def _push_delete_undo(sid: str, entry: dict, db_path: str) -> None:
    """将已删除条目压入撤销栈（去除内部字段 _row_idx）。"""
    with _undo_lock:
        buf = _undo_buffers.setdefault(sid, [])
        buf.append({
            'entry':    {k: v for k, v in entry.items() if k != '_row_idx'},
            'db_path':  db_path,
            'ts':       time.time(),
        })
        if len(buf) > _UNDO_MAX:
            buf.pop(0)


def _pop_delete_undo(sid: str):
    """弹出最近一次删除记录，没有可撤销项时返回 None。"""
    with _undo_lock:
        buf = _undo_buffers.get(sid, [])
        return buf.pop() if buf else None


# ── KB 冲突摘要字段 ───────────────────────────────────────
_CONFLICT_FIELDS = [
    '错误类型', '错误ID', '关键描述关键词', '报错原因',
    '所属模块', '录入日期', '_row_idx',
]

# ── 额外错误关键词 ────────────────────────────────────────
_EXTRA_PATTERNS_FILE    = BASE_DIR / 'extra_patterns.json'
_EXTRA_PATTERNS_DEFAULT = [
    'ERROR', 'FATAL', 'FAILED', 'VIRL_MEM_WARNING', 'JVP TEST FAILED',
    'SVA_ERROR', 'SVA_FATAL', 'SVA_WARNING',
]
_extra_patterns_lock    = threading.Lock()


def _load_extra_patterns() -> list:
    try:
        if _EXTRA_PATTERNS_FILE.exists():
            data = json.loads(_EXTRA_PATTERNS_FILE.read_text(encoding='utf-8'))
            if isinstance(data, list):
                return [str(x).strip().upper() for x in data if str(x).strip()]
    except Exception:
        pass
    return list(_EXTRA_PATTERNS_DEFAULT)


def _save_extra_patterns(patterns: list):
    _EXTRA_PATTERNS_FILE.write_text(
        json.dumps(patterns, ensure_ascii=False, indent=2), encoding='utf-8')


EXTRA_PATTERNS: list = _load_extra_patterns()

# ── 通过标记 ──────────────────────────────────────────────
_PASS_PATTERNS_FILE    = BASE_DIR / 'pass_patterns.json'
_PASS_PATTERNS_DEFAULT = ['JVP TEST PASSED']
_pass_patterns_lock    = threading.Lock()


def _load_pass_patterns() -> list:
    try:
        if _PASS_PATTERNS_FILE.exists():
            data = json.loads(_PASS_PATTERNS_FILE.read_text(encoding='utf-8'))
            if isinstance(data, list):
                return [str(x).strip() for x in data if str(x).strip()]
    except Exception:
        pass
    return list(_PASS_PATTERNS_DEFAULT)


def _save_pass_patterns(patterns: list):
    _PASS_PATTERNS_FILE.write_text(
        json.dumps(patterns, ensure_ascii=False, indent=2), encoding='utf-8')


PASS_PATTERNS: list = _load_pass_patterns()


# ── 工具函数 ──────────────────────────────────────────────

def _valid_levels() -> set:
    """返回所有合法的 level 值（UVM 三项 + 当前 EXTRA_PATTERNS）。"""
    return {'UVM_FATAL', 'UVM_ERROR', 'UVM_WARNING',
            'SVA_ERROR', 'SVA_FATAL', 'SVA_WARNING'} | set(EXTRA_PATTERNS)


def _normalize_error_level(level) -> str:
    return str(level or '').strip().upper()


_SV_NUMBER_RE = re.compile(r"(?<![A-Za-z_])[-+]?(?:\d+)?'s?[bodh][0-9a-f_xz?]+", re.IGNORECASE)
_HEX_NUMBER_RE = re.compile(r"(?<![A-Za-z_])[-+]?0x[0-9a-f_]+", re.IGNORECASE)
_DECIMAL_NUMBER_RE = re.compile(r"(?<![A-Za-z_])[-+]?\d[\d_]*(?:\.\d[\d_]*)?")


def _dedup_description_signature(description, limit=None) -> str:
    normalized = ' '.join(str(description or '').split()).lower()
    normalized = _SV_NUMBER_RE.sub('<num>', normalized)
    normalized = _HEX_NUMBER_RE.sub('<num>', normalized)
    normalized = _DECIMAL_NUMBER_RE.sub('<num>', normalized)
    if limit is not None:
        return normalized[:limit]
    return normalized


def _unique_errors_by_level(results: list) -> dict:
    entries_by_key = {}
    grouped = {}
    for r in results:
        fname = r.get('file', '')
        for err in r.get('all_errors', []):
            lvl = _normalize_error_level(err.get('level', ''))
            if not lvl:
                continue
            eid = str(err.get('error_id', '') or '').strip()
            loc = str(err.get('location', '') or '').strip()
            desc = str(err.get('description', '') or '')
            desc_sig = _dedup_description_signature(desc)

            key_parts = [lvl]
            if eid:
                key_parts.append(eid.lower())
            if loc:
                key_parts.append(loc.lower())
            key_parts.append(desc_sig)
            key = tuple(key_parts)

            entry = entries_by_key.get(key)
            if entry is None:
                entry = {
                    'error_id':    eid,
                    'description': desc,
                    'location':    loc,
                    'files':       [],
                }
                entries_by_key[key] = entry
                grouped.setdefault(lvl, []).append(entry)
            if fname and fname not in entry['files']:
                entry['files'].append(fname)
    for entries in grouped.values():
        entries.sort(key=lambda e: -len(e['files']))
    return grouped


def _unique_error_counts(results: list) -> dict:
    grouped = _unique_errors_by_level(results)
    counts = {level: len(errors) for level, errors in grouped.items()}
    for k in ('UVM_FATAL', 'UVM_ERROR', 'UVM_WARNING'):
        counts.setdefault(k, 0)
    return counts

def _conflict_summary(conflicts: list) -> list:
    """返回冲突条目的摘要字段列表，用于前端展示。"""
    return [{f: str(e.get(f, '') or '') for f in _CONFLICT_FIELDS} for e in conflicts]


def _validate_db_path(raw: str) -> str:
    """验证并规范化知识库路径。

    校验规则：
      - 空值返回 DB_DEFAULT
      - 非空值必须为绝对路径且以 .xlsx 结尾

    限制：本函数**不限制目录**——任何能通过 OS 权限读写的绝对 .xlsx 路径
    都接受（含 UNC、网络共享、用户家目录之外）。用户由 UI 提供路径，工具按
    其指令访问，由 OS 权限决定可达性。早期 docstring 写"防路径穿越"易引起
    误读，已在 2026-05-11 L-5 审查中纠正。

    不合法时抛出 ValueError（调用方返回 400）。
    """
    s = (raw or '').strip()
    if not s:
        return DB_DEFAULT
    p = Path(s)
    if p.suffix.lower() != '.xlsx':
        raise ValueError('知识库路径必须以 .xlsx 结尾')
    if not p.is_absolute():
        raise ValueError('知识库路径必须为绝对路径，不允许相对路径')
    return str(p)


def _get_results(sid: str):
    """读取会话数据，同时清理过期条目。"""
    with _store_lock:
        now = time.time()
        stale = [k for k, v in list(_store.items()) if now - v['ts'] > _STORE_TTL]
        for k in stale:
            del _store[k]
        entry = _store.get(sid)
        return (entry.get('results', []), entry.get('db_path', DB_DEFAULT)) if entry else ([], DB_DEFAULT)


def _set_results(sid: str, results: list, db_path: str, file_paths: list = None):
    with _store_lock:
        existing = _store.get(sid, {})
        _store[sid] = {
            'results':    results,
            'db_path':    db_path,
            'file_paths': file_paths or existing.get('file_paths', []),
            'p3_history': existing.get('p3_history', []),
            'p3_tokens':  existing.get('p3_tokens', 0),
            'ts':         time.time(),
        }


def _get_file_paths(sid: str) -> list:
    """返回当前会话的文件路径列表。

    两种模式都会填充（c552c5f 起）：
    - 路径模式：用户提供的实际路径（任意目录）
    - 上传模式：UPLOAD_DIR 下的 ``{sid}_{原文件名}`` 路径，文件保留至 24h 后由
      _cleanup_old_files 清理，用于 P2 AI 日志问答跨多次请求复用
    """
    with _store_lock:
        entry = _store.get(sid)
        return list(entry.get('file_paths', [])) if entry else []


def _get_p3_history(sid: str) -> list:
    """返回 P3 多轮对话历史。"""
    with _store_lock:
        entry = _store.get(sid)
        return list(entry.get('p3_history', [])) if entry else []


def _set_p3_history(sid: str, history: list) -> None:
    """更新 P3 多轮对话历史，不影响其他会话字段。"""
    with _store_lock:
        if sid in _store:
            _store[sid]['p3_history'] = list(history)
            _store[sid]['ts'] = time.time()


def _sid() -> str:
    if 'sid' not in session:
        session['sid'] = str(uuid.uuid4())
    return session['sid']


def _cleanup_old_files():
    """启动时清理 24 小时前的临时文件（uploads/ 和 reports/）。"""
    max_age = 24 * 3600
    now = time.time()
    for directory in (UPLOAD_DIR, REPORT_DIR):
        for fp in directory.iterdir():
            try:
                if now - fp.stat().st_mtime > max_age:
                    fp.unlink()
            except OSError:
                pass
