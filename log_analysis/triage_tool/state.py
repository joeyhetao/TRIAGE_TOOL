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

# ── 会话存储 ──────────────────────────────────────────────
_STORE_TTL  = 2 * 3600                       # 2 小时后过期
_store: dict = {}                            # sid -> {'results', 'db_path', 'ts'}
_store_lock  = threading.Lock()

# ── 后台任务状态 ──────────────────────────────────────────
_JOBS_TTL = 3600                             # 任务状态保留 1 小时
_jobs: dict = {}                             # job_id -> 任务字段

# ── KB 冲突摘要字段 ───────────────────────────────────────
_CONFLICT_FIELDS = [
    '错误类型', '错误ID', '关键描述关键词', '报错原因',
    '所属模块', '录入日期', '_row_idx',
]

# ── 额外错误关键词 ────────────────────────────────────────
_EXTRA_PATTERNS_FILE    = BASE_DIR / 'extra_patterns.json'
_EXTRA_PATTERNS_DEFAULT = ['ERROR', 'FATAL', 'FAILED', 'VIRL_MEM_WARNING', 'JVP TEST FAILED']
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
    return {'UVM_FATAL', 'UVM_ERROR', 'UVM_WARNING'} | set(EXTRA_PATTERNS)


def _unique_error_counts(results: list) -> dict:
    """跨所有文件对 all_errors 去重，返回各级别唯一错误数。"""
    seen   = set()
    counts = {}
    for r in results:
        for err in r.get('all_errors', []):
            lvl = err.get('level', '')
            eid = err.get('error_id', '').lower()
            key = (lvl, eid if eid else err.get('description', '')[:80].lower())
            if key not in seen:
                seen.add(key)
                counts[lvl] = counts.get(lvl, 0) + 1
    for k in ('UVM_FATAL', 'UVM_ERROR', 'UVM_WARNING'):
        counts.setdefault(k, 0)
    return counts


def _conflict_summary(conflicts: list) -> list:
    """返回冲突条目的摘要字段列表，用于前端展示。"""
    return [{f: str(e.get(f, '') or '') for f in _CONFLICT_FIELDS} for e in conflicts]


def _validate_db_path(raw: str) -> str:
    """验证并规范化知识库路径，防止路径穿越攻击。
    规则：空值返回 DB_DEFAULT；非空值必须为绝对路径且以 .xlsx 结尾。
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
        return (entry['results'], entry['db_path']) if entry else ([], DB_DEFAULT)


def _set_results(sid: str, results: list, db_path: str):
    with _store_lock:
        _store[sid] = {'results': results, 'db_path': db_path, 'ts': time.time()}


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
