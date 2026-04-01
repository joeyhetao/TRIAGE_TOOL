# -*- coding: utf-8 -*-
import re
from flask import Blueprint, request, jsonify

import state
from core.db_manager import load_db, find_duplicates, append_entry, update_entry, delete_entry

kb_bp = Blueprint('kb', __name__)


@kb_bp.route('/query', methods=['POST'])
def query_kb():
    """知识库模糊查询：按错误类型、错误ID（部分匹配）、关键词（任意词命中）搜索。"""
    data = request.get_json() or {}
    try:
        db_path = state._validate_db_path(data.get('db_path', ''))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    level    = data.get('level',    '').strip().upper()
    error_id = data.get('error_id', '').strip().lower()
    text     = data.get('text',     '').strip().lower()

    try:
        db_entries = load_db(db_path)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    tokens = [t for t in re.split(r'[\s,，]+', text) if t] if text else []

    SEARCH_FIELDS = ['错误ID', '关键描述关键词', '报错原因', '解决方案', '所属模块', '根因分类']
    scored = []
    for entry in db_entries:
        if level and str(entry.get('错误类型', '')).strip().upper() != level:
            continue
        if error_id and error_id not in str(entry.get('错误ID', '')).strip().lower():
            continue
        if tokens:
            blob = ' '.join(str(entry.get(f, '')) for f in SEARCH_FIELDS).lower()
            score = sum(1 for t in tokens if t in blob)
            if score == 0:
                continue
        else:
            score = 1
        scored.append((score, entry))

    scored.sort(key=lambda x: -x[0])
    entries = [e for _, e in scored[:100]]
    return jsonify({'entries': entries, 'total': len(scored)})


@kb_bp.route('/kb/add', methods=['POST'])
def kb_add():
    """直接向知识库追加一条新记录，不依赖会话。"""
    data = request.get_json() or {}
    try:
        db_path = state._validate_db_path(data.get('db_path', ''))
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    MAX_LEN = 500

    level = data.get('错误类型', '').strip().upper()
    if level and level not in state._valid_levels():
        valid_str = ' / '.join(sorted(state._valid_levels()))
        return jsonify({'success': False,
                        'error': f'错误类型无效，须为空（不限）或 {valid_str}'}), 400
    reason = data.get('报错原因', '').strip()
    if not reason:
        return jsonify({'success': False, 'error': '报错原因不能为空'}), 400

    entry = {
        '错误类型':       level,
        '错误ID':         data.get('错误ID',         '')[:MAX_LEN],
        '关键描述关键词':  data.get('关键描述关键词', '')[:MAX_LEN],
        '报错原因':        reason[:MAX_LEN],
        '所属模块':        data.get('所属模块',       '')[:MAX_LEN],
        '根因分类':        data.get('根因分类',       '')[:MAX_LEN],
        '解决方案':        data.get('解决方案',       '')[:MAX_LEN],
        '关联用例':        data.get('关联用例',       '')[:MAX_LEN],
        '录入人':          data.get('录入人',         '')[:MAX_LEN],
    }
    try:
        if not data.get('force'):
            conflicts = find_duplicates(db_path, entry)
            if conflicts:
                return jsonify({'success': False, 'duplicate': True,
                                'conflicts': state._conflict_summary(conflicts)})
        append_entry(db_path, entry)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@kb_bp.route('/kb/update', methods=['POST'])
def kb_update():
    """编辑知识库中指定行。JSON: {row_idx, db_path?, 字段名: 值, ...}"""
    data = request.get_json() or {}
    row_idx = data.get('row_idx')
    if not isinstance(row_idx, int) or row_idx < 2:
        return jsonify({'success': False, 'error': '无效的行号'})
    try:
        db_path = state._validate_db_path(data.get('db_path', ''))
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    allowed = {'错误类型', '错误ID', '关键描述关键词', '报错原因',
               '所属模块', '根因分类', '解决方案', '关联用例', '录入人'}
    new_data = {k: str(v)[:500] for k, v in data.items() if k in allowed}
    if not new_data:
        return jsonify({'success': False, 'error': '无有效字段'})
    try:
        if not data.get('force'):
            conflicts = find_duplicates(db_path, new_data, exclude_row_idx=row_idx)
            if conflicts:
                return jsonify({'success': False, 'duplicate': True,
                                'conflicts': state._conflict_summary(conflicts)})
        update_entry(db_path, row_idx, new_data)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@kb_bp.route('/kb/delete', methods=['POST'])
def kb_delete():
    """删除知识库中指定行。JSON: {row_idx, db_path?}"""
    data = request.get_json() or {}
    row_idx = data.get('row_idx')
    if not isinstance(row_idx, int) or row_idx < 2:
        return jsonify({'success': False, 'error': '无效的行号'})
    try:
        db_path = state._validate_db_path(data.get('db_path', ''))
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    try:
        delete_entry(db_path, row_idx)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})
