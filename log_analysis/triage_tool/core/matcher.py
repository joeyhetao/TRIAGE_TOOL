# -*- coding: utf-8 -*-
import re
from .db_manager import load_db


def match_error(error: dict, db_entries: list) -> dict:
    """
    两阶段匹配：
      Step1 - 错误ID精确匹配（忽略大小写）+ 错误类型一致；收集所有命中行
      Step2 - 关键描述关键词包含匹配（支持中英文逗号分隔）；收集所有命中行
    返回匹配结果字典，entries 为所有命中的知识库条目列表（可能多条）。
    """
    if not error:
        return {'status': 'no_error', 'entry': None, 'entries': []}

    level       = error.get('level', '').upper()
    error_id    = error.get('error_id', '').lower()
    description = error.get('description', '').lower()

    def _sort_by_date(entries):
        """按录入日期降序排列，日期格式 YYYY-MM-DD；缺失日期排最后。"""
        return sorted(entries,
                      key=lambda e: str(e.get('录入日期', '') or ''),
                      reverse=True)

    # Step1: 收集所有 错误ID + 错误类型 均匹配的行
    id_matches = [
        e for e in db_entries
        if str(e.get('错误ID', '')).strip().lower() == error_id
        and error_id
        and str(e.get('错误类型', '')).strip().upper() == level
    ]
    if id_matches:
        id_matches = _sort_by_date(id_matches)
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
        kw_matches = _sort_by_date(kw_matches)
        return {'status': 'matched', 'match_by': 'keywords',
                'entry': kw_matches[0], 'entries': kw_matches}

    return {'status': 'unmatched', 'entry': None, 'entries': []}


def run_match(parse_results: list, db_path: str) -> list:
    """
    对每个日志的前 TOP_N 条错误逐一执行知识库匹配。
    r['match'] 为汇总状态：
      - 有任意未匹配 → unmatched
      - 全部命中     → matched（取第一条的匹配结果）
      - 无错误       → no_error
    """
    db_entries = load_db(db_path)
    for result in parse_results:
        top_errors = result.get('top_errors', [])
        for error in top_errors:
            error['match'] = match_error(error, db_entries)

        if not top_errors:
            result['match'] = {'status': 'no_error', 'entry': None, 'entries': []}
        elif any(e['match']['status'] == 'unmatched' for e in top_errors):
            result['match'] = {'status': 'unmatched', 'entry': None, 'entries': []}
        else:
            result['match'] = top_errors[0]['match']

    return parse_results
