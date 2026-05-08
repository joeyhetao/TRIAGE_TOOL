# -*- coding: utf-8 -*-
import re
import sys
import json
import subprocess
from datetime import datetime
from pathlib import Path
from flask import Blueprint, request, jsonify

import state
from core.db_manager import (load_db, find_duplicates, append_entry,
                              update_entry, delete_entry, restore_backup)

_PICKER_TIMEOUT = 30   # 秒；用户没在 30 秒内交互对话框就 fallback
_PICKER_SCRIPT  = state.BASE_DIR / 'core' / 'file_picker.py'

kb_bp = Blueprint('kb', __name__)


@kb_bp.route('/pick_db_file', methods=['GET'])
def pick_db_file():
    """弹原生文件对话框选 KB 文件。tkinter 跑在子进程里，避免挂死请求线程。

    子进程脚本：core/file_picker.py。stdout 单行 JSON，30s 超时强制兜底。
    无 GUI / tkinter 缺失 / WSLg 未起 / 用户长时间不交互 — 任一情况
    都会通过 fallback 返回，前端 alert 提示手输绝对路径。
    """
    initial = (request.args.get('initial') or '').strip()
    if initial:
        p = Path(initial)
        initial_dir = str(p.parent if p.parent.exists() else state.BASE_DIR)
    else:
        initial_dir = str(state.BASE_DIR)

    try:
        result = subprocess.run(
            [sys.executable, str(_PICKER_SCRIPT), initial_dir],
            capture_output=True,
            text=True,
            timeout=_PICKER_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return jsonify({'ok': False,
                        'reason': '弹窗超时（{}s）：未在桌面找到对话框或长时间未交互'.format(_PICKER_TIMEOUT)})
    except Exception as e:
        return jsonify({'ok': False, 'reason': '弹窗启动失败: {}'.format(e)})

    out = (result.stdout or '').strip()
    err = (result.stderr or '').strip()
    try:
        data = json.loads(out)
    except Exception:
        snippet = (err or out)[:200]
        return jsonify({'ok': False, 'reason': '弹窗返回不可解析: ' + snippet})

    if not data.get('ok'):
        return jsonify({'ok': False, 'reason': data.get('reason', '未知错误')})

    path = data.get('path') or ''
    if not path:
        return jsonify({'ok': False, 'cancelled': True})

    return jsonify({'ok': True, 'path': str(Path(path).resolve())})


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
    MAX_LEN = state.MAX_LEN

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
    """删除知识库中指定行，并将完整条目压入当前 session 的撤销栈。
    JSON: {row_idx, db_path?}"""
    data = request.get_json() or {}
    row_idx = data.get('row_idx')
    if not isinstance(row_idx, int) or row_idx < 2:
        return jsonify({'success': False, 'error': '无效的行号'})
    try:
        db_path = state._validate_db_path(data.get('db_path', ''))
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    try:
        # 删除前先读出该行数据供撤销
        entries = load_db(db_path)
        entry_to_undo = next((e for e in entries if e.get('_row_idx') == row_idx), None)

        delete_entry(db_path, row_idx)

        # 压入撤销栈，返回 entry_label 供前端 Toast 显示
        entry_label = ''
        if entry_to_undo:
            sid = state._sid()
            state._push_delete_undo(sid, entry_to_undo, db_path)
            lvl = str(entry_to_undo.get('错误类型', '') or '').strip()
            eid = str(entry_to_undo.get('错误ID',   '') or '').strip()
            entry_label = f"{lvl} / {eid}" if eid else (lvl or '未知条目')

        return jsonify({'success': True, 'entry_label': entry_label})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@kb_bp.route('/kb/undo_delete', methods=['POST'])
def kb_undo_delete():
    """撤销最近一次删除操作（从当前 session 的撤销栈恢复条目）。"""
    sid = state._sid()
    item = state._pop_delete_undo(sid)
    if not item:
        return jsonify({'success': False, 'error': '没有可撤销的删除操作（会话已过期或未执行过删除）'})
    try:
        append_entry(item['db_path'], item['entry'])
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@kb_bp.route('/kb/backups')
def kb_backups():
    """列出当前知识库路径下可用的滚动备份文件及修改时间。"""
    try:
        db_path = state._validate_db_path(request.args.get('db_path', ''))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    p = Path(db_path)
    result = []
    for i in range(1, state.BACKUP_COUNT + 1):
        bak = p.parent / f"{p.stem}.bak{i}{p.suffix}"
        if bak.exists():
            mtime = bak.stat().st_mtime
            result.append({
                'idx':      i,
                'filename': bak.name,
                'mtime':    mtime,
                'mtime_str': datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M:%S'),
            })
    return jsonify({'backups': result})


@kb_bp.route('/kb/restore_backup', methods=['POST'])
def kb_restore():
    """从指定滚动备份恢复知识库。JSON: {backup_idx, db_path?}
    恢复前自动将当前文件轮转备份，不会丢失当前状态。"""
    data = request.get_json() or {}
    backup_idx = data.get('backup_idx')
    if not isinstance(backup_idx, int) or backup_idx < 1 or backup_idx > state.BACKUP_COUNT:
        return jsonify({'success': False, 'error': f'备份编号须在 1–{state.BACKUP_COUNT} 之间'})
    try:
        db_path = state._validate_db_path(data.get('db_path', ''))
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    try:
        restore_backup(db_path, backup_idx)
        return jsonify({'success': True})
    except (FileNotFoundError, ValueError) as e:
        return jsonify({'success': False, 'error': str(e)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
