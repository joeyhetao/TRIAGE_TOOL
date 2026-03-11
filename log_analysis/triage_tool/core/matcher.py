# -*- coding: utf-8 -*-
from .db_manager import load_db


def match_error(error: dict, db_entries: list) -> dict:
    """
    两阶段匹配：
      Step1 - 错误ID精确匹配（忽略大小写）+ 错误类型一致
      Step2 - 关键描述关键词包含匹配
    返回匹配结果字典。
    """
    if not error:
        return {'status': 'no_error', 'entry': None}

    level = error.get('level', '').upper()
    error_id = error.get('error_id', '').lower()
    description = error.get('description', '').lower()

    # Step1: 错误ID精确匹配
    for entry in db_entries:
        db_id = str(entry.get('错误ID', '')).strip().lower()
        db_level = str(entry.get('错误类型', '')).strip().upper()
        if db_id and db_id == error_id and db_level == level:
            return {'status': 'matched', 'match_by': 'error_id', 'entry': entry}

    # Step2: 关键词包含匹配
    for entry in db_entries:
        raw_keywords = str(entry.get('关键描述关键词', '')).strip()
        if not raw_keywords:
            continue
        keywords = [kw.strip().lower() for kw in raw_keywords.split(',') if kw.strip()]
        if keywords and all(kw in description for kw in keywords):
            return {'status': 'matched', 'match_by': 'keywords', 'entry': entry}

    return {'status': 'unmatched', 'entry': None}


def run_match(parse_results: list, db_path: str) -> list:
    """
    对所有日志的首错执行知识库匹配，返回含匹配结果的完整数据。
    """
    db_entries = load_db(db_path)
    for result in parse_results:
        result['match'] = match_error(result.get('first_error'), db_entries)
    return parse_results
