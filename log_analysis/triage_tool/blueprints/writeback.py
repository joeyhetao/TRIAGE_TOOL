# -*- coding: utf-8 -*-
from flask import Blueprint, request, jsonify

import state
from core.db_manager import find_duplicates, append_entry

writeback_bp = Blueprint('writeback', __name__)


@writeback_bp.route('/writeback', methods=['POST'])
def writeback():
    sid = state._sid()
    results, db_path = state._get_results(sid)
    data = request.get_json()

    MAX_LEN = 500
    level = data.get('level', '').strip().upper()
    if level not in state._valid_levels():
        return jsonify({'success': False, 'error': '无效的错误级别'}), 400
    reason = data.get('reason', '').strip()
    if not reason:
        return jsonify({'success': False, 'error': '报错原因不能为空'}), 400

    entry = {
        '错误类型':       level,
        '错误ID':         data.get('error_id',    '')[:MAX_LEN],
        '关键描述关键词':  data.get('keywords',    '')[:MAX_LEN],
        '报错原因':        reason[:MAX_LEN],
        '所属模块':        data.get('module',      '')[:MAX_LEN],
        '根因分类':        data.get('category',    '')[:MAX_LEN],
        '解决方案':        data.get('solution',    '')[:MAX_LEN],
        '关联用例':        data.get('related_case','')[:MAX_LEN],
        '录入人':          data.get('author',      '')[:MAX_LEN],
    }
    try:
        if not data.get('force'):
            conflicts = find_duplicates(db_path, entry)
            if conflicts:
                return jsonify({'success': False, 'duplicate': True,
                                'conflicts': state._conflict_summary(conflicts)})
        append_entry(db_path, entry)

        file_name = data.get('file_name', '')
        error_idx = int(data.get('error_idx', 0))
        for r in results:
            if r['file'] == file_name:
                top_errors = r.get('top_errors', [])
                if 0 <= error_idx < len(top_errors):
                    cur_match = top_errors[error_idx].get('match', {})
                    if cur_match.get('status') == 'matched':
                        existing = list(cur_match.get('entries') or
                                        ([cur_match['entry']] if cur_match.get('entry') else []))
                        top_errors[error_idx]['match'] = {
                            'status':   'matched',
                            'match_by': cur_match.get('match_by', 'manual'),
                            'entry':    cur_match['entry'],
                            'entries':  existing + [entry],
                        }
                    else:
                        top_errors[error_idx]['match'] = {
                            'status':   'matched',
                            'match_by': 'manual',
                            'entry':    entry,
                            'entries':  [entry],
                        }
                if not top_errors:
                    r['match'] = {'status': 'no_error', 'entry': None, 'entries': []}
                elif any(e['match']['status'] == 'unmatched' for e in top_errors):
                    r['match'] = {'status': 'unmatched', 'entry': None, 'entries': []}
                else:
                    r['match'] = top_errors[0]['match']
                break

        state._set_results(sid, results, db_path)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
