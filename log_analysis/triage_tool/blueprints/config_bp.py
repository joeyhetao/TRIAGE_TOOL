# -*- coding: utf-8 -*-
import re
from flask import Blueprint, request, jsonify

import state

config_bp = Blueprint('config_bp', __name__)


# ── 额外错误关键词 ────────────────────────────────────────

@config_bp.route('/extra_patterns', methods=['GET'])
def extra_patterns_get():
    """返回当前额外错误关键词列表。"""
    return jsonify({'patterns': state.EXTRA_PATTERNS})


@config_bp.route('/extra_patterns/add', methods=['POST'])
def extra_patterns_add():
    """添加一个关键词。JSON: {keyword: str}"""
    data = request.get_json() or {}
    kw = data.get('keyword', '').strip().upper()
    if not kw:
        return jsonify({'success': False, 'error': '关键词不能为空'}), 400
    if not re.match(r'^[A-Z0-9_ ]+$', kw):
        return jsonify({'success': False,
                        'error': '关键词只能包含大写字母、数字、下划线或空格'}), 400
    with state._extra_patterns_lock:
        if kw in state.EXTRA_PATTERNS:
            return jsonify({'success': False, 'error': '关键词已存在'})
        state.EXTRA_PATTERNS.append(kw)
        try:
            state._save_extra_patterns(state.EXTRA_PATTERNS)
        except Exception as e:
            state.EXTRA_PATTERNS.remove(kw)
            return jsonify({'success': False, 'error': str(e)}), 500
    return jsonify({'success': True, 'patterns': state.EXTRA_PATTERNS})


@config_bp.route('/extra_patterns/delete', methods=['POST'])
def extra_patterns_delete():
    """删除一个关键词。JSON: {keyword: str}"""
    data = request.get_json() or {}
    kw = data.get('keyword', '').strip().upper()
    with state._extra_patterns_lock:
        if kw not in state.EXTRA_PATTERNS:
            return jsonify({'success': False, 'error': '关键词不存在'})
        state.EXTRA_PATTERNS.remove(kw)
        try:
            state._save_extra_patterns(state.EXTRA_PATTERNS)
        except Exception as e:
            state.EXTRA_PATTERNS.append(kw)
            return jsonify({'success': False, 'error': str(e)}), 500
    return jsonify({'success': True, 'patterns': state.EXTRA_PATTERNS})


@config_bp.route('/extra_patterns/update', methods=['POST'])
def extra_patterns_update():
    """重命名一个关键词。JSON: {old: str, new: str}"""
    data = request.get_json() or {}
    old_kw = data.get('old', '').strip().upper()
    new_kw = data.get('new', '').strip().upper()
    if not new_kw:
        return jsonify({'success': False, 'error': '新关键词不能为空'}), 400
    if not re.match(r'^[A-Z0-9_]+$', new_kw):
        return jsonify({'success': False,
                        'error': '关键词只能包含大写字母、数字和下划线'}), 400
    with state._extra_patterns_lock:
        if old_kw not in state.EXTRA_PATTERNS:
            return jsonify({'success': False, 'error': '原关键词不存在'})
        if new_kw in state.EXTRA_PATTERNS and new_kw != old_kw:
            return jsonify({'success': False, 'error': '新关键词已存在'})
        idx = state.EXTRA_PATTERNS.index(old_kw)
        state.EXTRA_PATTERNS[idx] = new_kw
        try:
            state._save_extra_patterns(state.EXTRA_PATTERNS)
        except Exception as e:
            state.EXTRA_PATTERNS[idx] = old_kw
            return jsonify({'success': False, 'error': str(e)}), 500
    return jsonify({'success': True, 'patterns': state.EXTRA_PATTERNS})


# ── 通过标记 ──────────────────────────────────────────────

@config_bp.route('/pass_patterns', methods=['GET'])
def pass_patterns_get():
    """返回当前通过标记字符串列表。"""
    return jsonify({'patterns': state.PASS_PATTERNS})


@config_bp.route('/pass_patterns/add', methods=['POST'])
def pass_patterns_add():
    """添加一个通过标记。JSON: {pattern: str}"""
    data = request.get_json() or {}
    pt = data.get('pattern', '').strip()
    if not pt:
        return jsonify({'success': False, 'error': '通过标记不能为空'}), 400
    with state._pass_patterns_lock:
        if pt in state.PASS_PATTERNS:
            return jsonify({'success': False, 'error': '该标记已存在'})
        state.PASS_PATTERNS.append(pt)
        try:
            state._save_pass_patterns(state.PASS_PATTERNS)
        except Exception as e:
            state.PASS_PATTERNS.remove(pt)
            return jsonify({'success': False, 'error': str(e)}), 500
    return jsonify({'success': True, 'patterns': state.PASS_PATTERNS})


@config_bp.route('/pass_patterns/delete', methods=['POST'])
def pass_patterns_delete():
    """删除一个通过标记。JSON: {pattern: str}"""
    data = request.get_json() or {}
    pt = data.get('pattern', '').strip()
    with state._pass_patterns_lock:
        if pt not in state.PASS_PATTERNS:
            return jsonify({'success': False, 'error': '标记不存在'})
        state.PASS_PATTERNS.remove(pt)
        try:
            state._save_pass_patterns(state.PASS_PATTERNS)
        except Exception as e:
            state.PASS_PATTERNS.append(pt)
            return jsonify({'success': False, 'error': str(e)}), 500
    return jsonify({'success': True, 'patterns': state.PASS_PATTERNS})


@config_bp.route('/pass_patterns/update', methods=['POST'])
def pass_patterns_update():
    """修改一个通过标记。JSON: {old: str, new: str}"""
    data = request.get_json() or {}
    old_pt = data.get('old', '').strip()
    new_pt = data.get('new', '').strip()
    if not new_pt:
        return jsonify({'success': False, 'error': '新标记不能为空'}), 400
    with state._pass_patterns_lock:
        if old_pt not in state.PASS_PATTERNS:
            return jsonify({'success': False, 'error': '原标记不存在'})
        if new_pt in state.PASS_PATTERNS and new_pt != old_pt:
            return jsonify({'success': False, 'error': '新标记已存在'})
        idx = state.PASS_PATTERNS.index(old_pt)
        state.PASS_PATTERNS[idx] = new_pt
        try:
            state._save_pass_patterns(state.PASS_PATTERNS)
        except Exception as e:
            state.PASS_PATTERNS[idx] = old_pt
            return jsonify({'success': False, 'error': str(e)}), 500
    return jsonify({'success': True, 'patterns': state.PASS_PATTERNS})
