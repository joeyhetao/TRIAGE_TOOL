# -*- coding: utf-8 -*-
"""tests/test_kb_stats.py — KB 活跃度事件层单元测试。"""
import time
import json
import pytest

from core import kb_stats


@pytest.fixture
def db_path(tmp_path):
    """tmp_path 下假装放一个 KB；事件文件会落在它旁边的 kb_hits.jsonl。"""
    p = tmp_path / 'error_db.xlsx'
    p.touch()       # 不需要真 Excel，kb_stats 只用它的目录
    return str(p)


# ── 基础 record / aggregate ──────────────────────────────

def test_record_then_aggregate(db_path):
    kb_stats.record_event('idA', 'match', db_path)
    kb_stats.record_event('idA', 'match', db_path)
    kb_stats.record_event('idB', 'writeback', db_path)
    stats = kb_stats.aggregate_stats(db_path)
    assert stats['idA']['hit_total'] == 2
    assert stats['idA']['hit_7d']    == 2
    assert stats['idA']['hit_30d']   == 2
    assert stats['idB']['hit_total'] == 1


def test_empty_stable_id_silently_ignored(db_path):
    kb_stats.record_event('', 'match', db_path)
    kb_stats.record_event('   ', 'match', db_path)
    kb_stats.record_event(None, 'match', db_path)
    assert kb_stats.aggregate_stats(db_path) == {}


def test_get_stats_for_unknown_returns_empty_struct(db_path):
    s = kb_stats.get_stats_for('nonexistent', db_path)
    assert s['hit_total'] == 0
    assert s['days_since_last'] == -1


# ── 时间窗（hit_7d / hit_30d / days_since_last）──────────

def test_time_windows(db_path):
    now = time.time()
    kb_stats.record_event('id1', 'match', db_path, ts=now)                    # 今天
    kb_stats.record_event('id1', 'match', db_path, ts=now - 5  * 86400)       # 5 天前
    kb_stats.record_event('id1', 'match', db_path, ts=now - 20 * 86400)       # 20 天前
    kb_stats.record_event('id1', 'match', db_path, ts=now - 60 * 86400)       # 60 天前
    s = kb_stats.aggregate_stats(db_path)['id1']
    assert s['hit_total'] == 4
    assert s['hit_7d']    == 2     # 今天 + 5 天前
    assert s['hit_30d']   == 3     # 今天 + 5 天前 + 20 天前
    assert s['days_since_last'] == 0   # 最后一笔是今天


def test_days_since_last_for_old_entries(db_path):
    kb_stats.record_event('cold', 'match', db_path, ts=time.time() - 100 * 86400)
    s = kb_stats.aggregate_stats(db_path)['cold']
    assert s['days_since_last'] == 100
    assert s['hit_7d']  == 0
    assert s['hit_30d'] == 0


# ── activity_boost 公式 ─────────────────────────────────

def test_activity_boost_caps_at_1():
    s = {'hit_7d': 100}
    assert kb_stats.activity_boost(s) == 1.0


def test_activity_boost_zero_for_no_hits():
    assert kb_stats.activity_boost({}) == 0.0
    assert kb_stats.activity_boost({'hit_7d': 0}) == 0.0


def test_activity_boost_linear_under_threshold():
    assert abs(kb_stats.activity_boost({'hit_7d': 3}) - 0.6) < 1e-9


# ── 缓存 mtime 失效 ──────────────────────────────────────

def test_cache_invalidated_after_new_event(db_path):
    kb_stats.record_event('x', 'match', db_path)
    s1 = kb_stats.aggregate_stats(db_path)
    assert s1['x']['hit_total'] == 1
    # 写新事件，确保 mtime 不同（同 100ns 写入可能未走文件系统粒度）
    time.sleep(0.01)
    kb_stats.record_event('x', 'match', db_path)
    s2 = kb_stats.aggregate_stats(db_path)
    assert s2['x']['hit_total'] == 2


# ── 归档 ─────────────────────────────────────────────────

def test_archive_old_events_moves_only_old(db_path):
    now = time.time()
    kb_stats.record_event('keep',    'match', db_path, ts=now - 10 * 86400)
    kb_stats.record_event('archive', 'match', db_path, ts=now - 200 * 86400)
    moved = kb_stats.archive_old_events(db_path, cutoff_days=180)
    assert moved == 1
    after = kb_stats.aggregate_stats(db_path)
    assert 'keep' in after
    assert 'archive' not in after


def test_archive_with_no_old_events_returns_zero(db_path):
    kb_stats.record_event('fresh', 'match', db_path)
    assert kb_stats.archive_old_events(db_path, cutoff_days=180) == 0


# ── 容错 ─────────────────────────────────────────────────

def test_corrupt_lines_are_skipped(db_path, tmp_path):
    """事件文件里有格式坏行时，仍能聚合好的部分。"""
    fp = tmp_path / 'kb_hits.jsonl'
    fp.write_text(
        '{"id":"good","ts":' + str(time.time()) + ',"src":"match"}\n'
        '*** corrupt line ***\n'
        '{"id":"good","ts":' + str(time.time()) + ',"src":"match"}\n',
        encoding='utf-8',
    )
    s = kb_stats.aggregate_stats(db_path)
    assert s['good']['hit_total'] == 2


def test_missing_file_returns_empty(db_path):
    """事件文件不存在时 aggregate_stats 返回空 dict 不抛异常。"""
    assert kb_stats.aggregate_stats(db_path) == {}


# ── 并发 record 不丢事件 ─────────────────────────────────

def test_concurrent_record_does_not_lose_events(db_path):
    import threading
    threads = [threading.Thread(target=kb_stats.record_event,
                                args=('concurrent', 'match', db_path))
               for _ in range(20)]
    for t in threads: t.start()
    for t in threads: t.join()
    s = kb_stats.aggregate_stats(db_path)
    assert s['concurrent']['hit_total'] == 20
