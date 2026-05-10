# -*- coding: utf-8 -*-
"""
KB 活跃度事件统计层

事件文件：与 error_db.xlsx 同目录的 kb_hits.jsonl，每行一个 JSON 对象：
    {"id":"abc123def456", "ts":1715000000.123, "src":"match"}

公开接口：
    record_event(stable_id, source, db_path, ts=None) -> None
    aggregate_stats(db_path) -> dict
        返回 {stable_id: {hit_total, hit_7d, hit_30d, last_hit_ts, first_hit_ts,
                          last_hit_iso, first_hit_iso}}
    get_stats_for(stable_id, db_path) -> dict   # 单条；零值返回固定结构
    archive_old_events(db_path, cutoff_days=180) -> int  # 将早于 cutoff 的事件挪到 archive

并发安全：复用 db_manager._FileLock，与 KB 写入锁分开（不同文件）。
聚合性能：mtime 缓存，事件文件未变就直接返回缓存；写事件后下次读自动失效。
降级：事件文件缺失 / 损坏行 / IO 异常 → 返回空字典或部分结果，不崩。
"""
import json
import time
import threading
from datetime import datetime, timezone
from pathlib import Path

from .db_manager import _FileLock

_thread_lock = threading.Lock()
_cache = {}            # {hits_file_path_str: (mtime_ns, aggregated_dict)}
_cache_lock = threading.Lock()


def _hits_path(db_path: str) -> Path:
    return Path(db_path).parent / 'kb_hits.jsonl'


def _archive_path(db_path: str) -> Path:
    return Path(db_path).parent / 'kb_hits_archive.jsonl'


def _empty_stats() -> dict:
    return {
        'hit_total': 0, 'hit_7d': 0, 'hit_30d': 0,
        'last_hit_ts': 0, 'first_hit_ts': 0,
        'last_hit_iso': '', 'first_hit_iso': '',
        'days_since_last': -1,
    }


def _ts_to_iso(ts: float) -> str:
    if not ts:
        return ''
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec='seconds')
    except (OverflowError, OSError, ValueError):
        return ''


# ── 事件写入 ──────────────────────────────────────────────

def record_event(stable_id: str, source: str, db_path: str, ts: float = None) -> None:
    """记录一次活跃度事件（append-only）。
    无效 stable_id（空字符串）静默忽略。
    """
    sid = (stable_id or '').strip()
    if not sid:
        return
    record = {
        'id':  sid,
        'ts':  ts if ts is not None else time.time(),
        'src': source or 'unknown',
    }
    line = json.dumps(record, ensure_ascii=False) + '\n'
    fp = _hits_path(db_path)
    try:
        with _thread_lock:
            with _FileLock(str(fp)):
                with open(fp, 'a', encoding='utf-8') as f:
                    f.write(line)
    except Exception:
        # 事件采集失败不阻塞主流程
        pass


# ── 聚合读取 ──────────────────────────────────────────────

def aggregate_stats(db_path: str) -> dict:
    """读事件文件，按 stable_id 聚合统计。mtime 未变时走缓存。"""
    fp = _hits_path(db_path)
    if not fp.exists():
        return {}
    try:
        mtime = fp.stat().st_mtime_ns
    except OSError:
        return {}

    cache_key = str(fp)
    with _cache_lock:
        cached = _cache.get(cache_key)
        if cached and cached[0] == mtime:
            return cached[1]

    now        = time.time()
    cutoff_7d  = now - 7  * 86400
    cutoff_30d = now - 30 * 86400
    stats: dict = {}

    try:
        with open(fp, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                sid = rec.get('id')
                if not sid:
                    continue
                ts = float(rec.get('ts', 0) or 0)
                s = stats.setdefault(sid, {
                    'hit_total': 0, 'hit_7d': 0, 'hit_30d': 0,
                    'last_hit_ts': 0, 'first_hit_ts': 0,
                })
                s['hit_total'] += 1
                if ts >= cutoff_7d:
                    s['hit_7d'] += 1
                if ts >= cutoff_30d:
                    s['hit_30d'] += 1
                if ts > s['last_hit_ts']:
                    s['last_hit_ts'] = ts
                if s['first_hit_ts'] == 0 or (ts and ts < s['first_hit_ts']):
                    s['first_hit_ts'] = ts
    except OSError:
        return {}

    for s in stats.values():
        s['last_hit_iso']  = _ts_to_iso(s['last_hit_ts'])
        s['first_hit_iso'] = _ts_to_iso(s['first_hit_ts'])
        s['days_since_last'] = int((now - s['last_hit_ts']) // 86400) if s['last_hit_ts'] else -1

    with _cache_lock:
        _cache[cache_key] = (mtime, stats)
    return stats


def get_stats_for(stable_id: str, db_path: str) -> dict:
    """单条统计；不存在或无效 ID 时返回零值字典。"""
    sid = (stable_id or '').strip()
    if not sid:
        return _empty_stats()
    return dict(aggregate_stats(db_path).get(sid) or _empty_stats())


# ── 归档 ──────────────────────────────────────────────────

def archive_old_events(db_path: str, cutoff_days: int = 180) -> int:
    """把早于 cutoff_days 的事件挪到 kb_hits_archive.jsonl。
    返回归档的事件数；主文件保留新事件 + 解析失败行（防误删）。
    """
    fp  = _hits_path(db_path)
    arc = _archive_path(db_path)
    if not fp.exists():
        return 0
    cutoff = time.time() - cutoff_days * 86400

    with _thread_lock:
        with _FileLock(str(fp)):
            keep, archive = [], []
            try:
                with open(fp, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except Exception:
                            keep.append(line)        # 保守：保留无法解析的行
                            continue
                        ts = float(rec.get('ts', 0) or 0)
                        (archive if ts < cutoff else keep).append(line)
            except OSError:
                return 0

            if not archive:
                return 0

            with open(fp, 'w', encoding='utf-8') as f:
                for line in keep:
                    f.write(line + '\n')
            with open(arc, 'a', encoding='utf-8') as f:
                for line in archive:
                    f.write(line + '\n')

    with _cache_lock:
        _cache.pop(str(fp), None)
    return len(archive)


# ── 派生工具：便利函数 ────────────────────────────────────

def days_since_last_hit(stats_entry: dict) -> int:
    """从聚合 stats[sid] 字典算出"上次命中 N 天前"，无命中返回 -1。"""
    last_ts = (stats_entry or {}).get('last_hit_ts') or 0
    if not last_ts:
        return -1
    return int((time.time() - last_ts) // 86400)


def activity_boost(stats_entry: dict, max_hits_7d: int = 5) -> float:
    """给排序公式提供的活跃度 boost：min(hit_7d / max_hits_7d, 1.0)。"""
    hit_7d = (stats_entry or {}).get('hit_7d') or 0
    return min(hit_7d / float(max_hits_7d), 1.0) if max_hits_7d > 0 else 0.0
