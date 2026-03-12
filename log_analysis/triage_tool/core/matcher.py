# -*- coding: utf-8 -*-
import re
from .db_manager import load_db


def match_error(error: dict, db_entries: list) -> dict:
    """
    两阶段匹配：
      Step1 - 错误ID精确匹配（忽略大小写）+ 错误类型一致
      Step2 - 关键描述关键词包含匹配（支持中英文逗号分隔）
    返回匹配结果字典。
    """
    if not error:
        return {'status': 'no_error', 'entry': None}

    level       = error.get('level', '').upper()
    error_id    = error.get('error_id', '').lower()
    description = error.get('description', '').lower()

    # Step1: 错误ID精确匹配
    for entry in db_entries:
        db_id    = str(entry.get('错误ID',   '')).strip().lower()
        db_level = str(entry.get('错误类型', '')).strip().upper()
        if db_id and db_id == error_id and db_level == level:
            return {'status': 'matched', 'match_by': 'error_id', 'entry': entry}

    # Step2: 关键词包含匹配（同时支持英文逗号 , 和中文逗号 ，）
    for entry in db_entries:
        raw_keywords = str(entry.get('关键描述关键词', '')).strip()
        if not raw_keywords:
            continue
        keywords = [kw.strip().lower()
                    for kw in re.split(r'[,，]', raw_keywords) if kw.strip()]
        if keywords and all(kw in description for kw in keywords):
            return {'status': 'matched', 'match_by': 'keywords', 'entry': entry}

    return {'status': 'unmatched', 'entry': None}


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
            result['match'] = {'status': 'no_error', 'entry': None}
        elif any(e['match']['status'] == 'unmatched' for e in top_errors):
            result['match'] = {'status': 'unmatched', 'entry': None}
        else:
            result['match'] = top_errors[0]['match']

    return parse_results
