# -*- coding: utf-8 -*-
import re
from .db_manager import load_db
from . import kb_stats


def match_error(error: dict, db_entries: list, stats: dict = None) -> dict:
    """
    两阶段匹配：
      Step1 - 错误ID精确匹配（忽略大小写）+ 错误类型一致；收集所有命中行
      Step2 - 关键描述关键词包含匹配（支持中英文逗号分隔）；收集所有命中行
    多条命中时按 (活跃度 boost desc, 录入日期 desc) 排序——活跃条目优先。
    stats=None 时退化为纯日期降序（向后兼容）。
    """
    if not error:
        return {'status': 'no_error', 'entry': None, 'entries': []}

    level       = error.get('level', '').upper()
    error_id    = error.get('error_id', '').lower()
    description = error.get('description', '').lower()

    def _sort_by_relevance(entries):
        """活跃度 boost desc + 录入日期 desc。stats=None 时 boost 全 0，退化为纯日期排序。"""
        def _key(e):
            date_part = str(e.get('录入日期', '') or '0000-00-00')
            if stats is None:
                return (0.0, date_part)
            sid = e.get('稳定ID', '')
            return (kb_stats.activity_boost(stats.get(sid)), date_part)
        return sorted(entries, key=_key, reverse=True)

    # Step1: 收集所有 错误ID + 错误类型 均匹配的行
    # 错误类型为空（不限）的 KB 条目可匹配任意 level
    id_matches = [
        e for e in db_entries
        if str(e.get('错误ID', '')).strip().lower() == error_id
        and error_id
        and (not str(e.get('错误类型', '')).strip()
             or str(e.get('错误类型', '')).strip().upper() == level)
    ]
    if id_matches:
        id_matches = _sort_by_relevance(id_matches)
        return {'status': 'matched', 'match_by': 'error_id',
                'entry': id_matches[0], 'entries': id_matches}

    # Step2: 收集所有关键词全部命中的行（支持英文逗号 , 和中文逗号 ，）
    kw_matches = []
    for entry in db_entries:
        raw_keywords = str(entry.get('关键描述关键词', '')).strip()
        if not raw_keywords:
            continue
        keywords = [kw.strip().lower()
                    for kw in re.split(r'[,，]', raw_keywords) if kw.strip()]
        if keywords and all(kw in description for kw in keywords):
            kw_matches.append(entry)
    if kw_matches:
        kw_matches = _sort_by_relevance(kw_matches)
        return {'status': 'matched', 'match_by': 'keywords',
                'entry': kw_matches[0], 'entries': kw_matches}

    return {'status': 'unmatched', 'entry': None, 'entries': []}


def run_match(parse_results: list, db_path: str, progress_cb=None) -> list:
    """
    对每个日志的前 TOP_N 条错误逐一执行知识库匹配。
    r['match'] 为汇总状态：
      - 有任意未匹配 → unmatched
      - 全部命中     → matched（取第一条的匹配结果）
      - 无错误       → no_error
    progress_cb(filename, matched, n_errors, done, total) — 每完成一个文件后调用。
    """
    db_entries = load_db(db_path)
    # 本次分析使用启动时刻的 stats 快照；本轮命中产生的事件影响下一轮排序
    stats_snapshot = kb_stats.aggregate_stats(db_path)
    # 把活跃度统计注入到每条 KB entry，下游模板/导出器可直接通过 e['_stats'] 读取
    for e in db_entries:
        sid = e.get('稳定ID', '')
        e['_stats'] = stats_snapshot.get(sid) or {}
    total = len(parse_results)
    for i, result in enumerate(parse_results):
        top_errors = result.get('top_errors', [])
        for error in top_errors:
            error['match'] = match_error(error, db_entries, stats=stats_snapshot)
            # 活跃度事件：每条命中的 KB 行记一笔
            entry = error['match'].get('entry') if error['match'].get('status') == 'matched' else None
            if entry:
                kb_stats.record_event(entry.get('稳定ID', ''), 'match', db_path)

        if not top_errors:
            result['match'] = {'status': 'no_error', 'entry': None, 'entries': []}
        elif any(e['match']['status'] == 'unmatched' for e in top_errors):
            result['match'] = {'status': 'unmatched', 'entry': None, 'entries': []}
        else:
            result['match'] = top_errors[0]['match']

        if progress_cb:
            matched = sum(1 for e in top_errors if e['match']['status'] == 'matched')
            progress_cb(result.get('file', ''), matched, len(top_errors), i + 1, total)

    return parse_results


def score_query(entries: list, text: str, level: str = '', stats: dict = None) -> list:
    """
    token 重叠打分 + 可选活跃度 boost，返回按 final_score 降序的 (score, entry) 列表。
    final_score = relevance × (1 + 0.5 × activity_boost)
    level 非空时仅包含匹配该错误类型的条目。
    stats=None 时退化为纯 token 重叠（向后兼容）。
    供 llm_bp 的 P4（similar_errors）和 P6（semantic_query）使用。
    """
    tokens = [t for t in re.split(r'[\s,，]+', text.lower()) if t] if text else []
    _SEARCH_FIELDS = ['错误ID', '关键描述关键词', '报错原因', '解决方案', '所属模块', '根因分类']
    scored = []
    for entry in entries:
        if level and str(entry.get('错误类型', '')).strip().upper() != level.upper():
            continue
        if tokens:
            blob = ' '.join(str(entry.get(f, '')) for f in _SEARCH_FIELDS).lower()
            relevance = sum(1 for t in tokens if t in blob)
            if relevance == 0:
                continue
        else:
            relevance = 1
        if stats:
            sid = entry.get('稳定ID', '')
            boost = kb_stats.activity_boost(stats.get(sid))
        else:
            boost = 0.0
        final = relevance * (1.0 + 0.5 * boost)
        scored.append((final, entry))
    scored.sort(key=lambda x: -x[0])
    return scored
