# -*- coding: utf-8 -*-
import os
import re
import sys
import io
import time
import uuid
import secrets
import glob as _glob
from pathlib import Path
from datetime import datetime
from flask import (Flask, render_template, request,
                   jsonify, send_file, session)
from werkzeug.utils import secure_filename

# ── Linux 终端编码修正 ────────────────────────────────────
if hasattr(sys.stdout, 'buffer'):
    sys.stdout = io.TextIOWrapper(
        sys.stdout.buffer, encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'buffer'):
    sys.stderr = io.TextIOWrapper(
        sys.stderr.buffer, encoding='utf-8', errors='replace')

from core.log_parser import parse_logs
from core.matcher    import run_match
from core.db_manager import append_entry, ensure_db
from core.reporter   import generate_excel, generate_html

# ── 初始化路径 ────────────────────────────────────────────
if getattr(sys, 'frozen', False):
    BASE_DIR    = Path(sys.executable).parent
    _BUNDLE_DIR = Path(sys._MEIPASS)
else:
    BASE_DIR    = Path(__file__).parent
    _BUNDLE_DIR = BASE_DIR

UPLOAD_DIR = BASE_DIR / 'uploads'
REPORT_DIR = BASE_DIR / 'reports'
DB_DEFAULT = str(BASE_DIR / 'error_db.xlsx')

UPLOAD_DIR.mkdir(exist_ok=True)
REPORT_DIR.mkdir(exist_ok=True)

# F4: 单文件大小上限 10 GB
MAX_FILE_SIZE = 10 * 1024 * 1024 * 1024
# 路径模式一次最多分析文件数
MAX_PATH_FILES = 5000

app = Flask(__name__,
            template_folder=str(_BUNDLE_DIR / 'templates'),
            static_folder=str(_BUNDLE_DIR / 'static'))

# S2: 持久化随机 secret_key（重启后 session 仍有效）
_key_file = BASE_DIR / '.secret_key'
if _key_file.exists():
    app.secret_key = _key_file.read_bytes()
else:
    _key = secrets.token_bytes(32)
    _key_file.write_bytes(_key)
    # Linux 多用户环境下限制为仅属主可读，防止 session 伪造
    if sys.platform != 'win32':
        os.chmod(str(_key_file), 0o600)
    app.secret_key = _key

# ── M4: 带 TTL 的会话数据存储 ────────────────────────────
_STORE_TTL = 2 * 3600   # 2小时后自动过期
_store: dict = {}        # sid -> {'results': list, 'db_path': str, 'ts': float}


def _get_results(sid: str):
    """读取会话数据，同时清理过期条目。"""
    now = time.time()
    stale = [k for k, v in list(_store.items()) if now - v['ts'] > _STORE_TTL]
    for k in stale:
        del _store[k]
    entry = _store.get(sid)
    return (entry['results'], entry['db_path']) if entry else ([], DB_DEFAULT)


def _set_results(sid: str, results: list, db_path: str):
    _store[sid] = {'results': results, 'db_path': db_path, 'ts': time.time()}


def _sid():
    if 'sid' not in session:
        session['sid'] = str(uuid.uuid4())
    return session['sid']


# ── F3: 启动时清理 24 小时前的临时文件 ──────────────────
def _cleanup_old_files():
    max_age = 24 * 3600
    now = time.time()
    for directory in (UPLOAD_DIR, REPORT_DIR):
        for fp in directory.iterdir():
            try:
                if now - fp.stat().st_mtime > max_age:
                    fp.unlink()
            except OSError:
                pass

_cleanup_old_files()


# ── 路由 ─────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html', db_default=DB_DEFAULT)


@app.route('/analyze', methods=['POST'])
def analyze():
    sid = _sid()

    db_path = request.form.get('db_path', '').strip() or DB_DEFAULT
    ensure_db(db_path)

    path_mode = request.form.get('path_mode', '').strip()

    if path_mode:
        # ── 路径模式：直接读服务器本地文件，无需上传拷贝 ──────
        raw = request.form.get('log_paths', '').strip()
        if not raw:
            return jsonify({'error': '请输入日志文件路径'}), 400

        # 支持换行或逗号分隔的多路径 / glob 表达式
        patterns = [p.strip()
                    for line in raw.splitlines()
                    for p in line.split(',')
                    if p.strip()]

        saved_paths = []
        not_found = []
        for pattern in patterns:
            matched = sorted(_glob.glob(pattern, recursive=True))
            log_files = [str(Path(fp).resolve())
                         for fp in matched
                         if Path(fp).is_file()
                         and Path(fp).suffix.lower() == '.log']
            if log_files:
                saved_paths.extend(log_files)
            else:
                not_found.append(pattern)

        if not saved_paths:
            return jsonify({'error': '未找到任何 .log 文件：' + '；'.join(not_found)}), 400

        if len(saved_paths) > MAX_PATH_FILES:
            return jsonify({
                'error': f'一次最多分析 {MAX_PATH_FILES} 个文件，'
                         f'当前匹配到 {len(saved_paths)} 个，请缩小范围'
            }), 400

    else:
        # ── 上传模式：浏览器上传文件流，保存到 uploads/ ────────
        files = request.files.getlist('logs')
        if not files or all(f.filename == '' for f in files):
            return jsonify({'error': '请至少上传一个日志文件'}), 400

        saved_paths = []
        for f in files:
            if f.filename == '':
                continue

            # F4: 检查单文件大小
            f.seek(0, 2)
            file_size = f.tell()
            f.seek(0)
            if file_size > MAX_FILE_SIZE:
                return jsonify({'error': f'文件 {f.filename} 超过10GB大小限制'}), 400

            # S1: 使用 secure_filename 防路径穿越
            safe_name = secure_filename(f.filename) or f'file_{uuid.uuid4().hex[:8]}.log'
            save_path = UPLOAD_DIR / f'{sid}_{safe_name}'
            f.save(str(save_path))
            saved_paths.append(str(save_path))

        if not saved_paths:
            return jsonify({'error': '文件保存失败'}), 500

    results = parse_logs(saved_paths)
    results = run_match(results, db_path)

    # 上传模式：解析完成后立即删除临时文件（结果已存入内存，文件不再需要）
    if not path_mode:
        for fp in saved_paths:
            try:
                Path(fp).unlink()
            except OSError:
                pass

    # 还原显示文件名（去掉 sid 前缀；路径模式下直接使用 basename）
    for r in results:
        r['file'] = Path(r['file']).name
        sid_prefix = f'{sid}_'
        if r['file'].startswith(sid_prefix):
            r['file'] = r['file'][len(sid_prefix):]

    _set_results(sid, results, db_path)
    return jsonify({'redirect': '/result'})


@app.route('/result')
def result():
    sid = _sid()
    results, db_path = _get_results(sid)
    return render_template('result.html', results=results, db_path=db_path)


@app.route('/writeback', methods=['POST'])
def writeback():
    sid = _sid()
    results, db_path = _get_results(sid)
    data = request.get_json()

    # S4: 输入校验
    VALID_LEVELS = {'UVM_FATAL', 'UVM_ERROR', 'UVM_WARNING'}
    MAX_LEN = 500
    level = data.get('level', '').strip().upper()
    if level not in VALID_LEVELS:
        return jsonify({'success': False, 'error': '无效的错误级别'}), 400
    reason = data.get('reason', '').strip()
    if not reason:
        return jsonify({'success': False, 'error': '报错原因不能为空'}), 400

    entry = {
        '错误类型':      level,
        '错误ID':        data.get('error_id',     '')[:MAX_LEN],
        '关键描述关键词': data.get('keywords',     '')[:MAX_LEN],
        '报错原因':      reason[:MAX_LEN],
        '所属模块':      data.get('module',        '')[:MAX_LEN],
        '根因分类':      data.get('category',      '')[:MAX_LEN],
        '解决方案':      data.get('solution',      '')[:MAX_LEN],
        '关联用例':      data.get('related_case',  '')[:MAX_LEN],
        '录入人':        data.get('author',        '')[:MAX_LEN],
    }
    try:
        append_entry(db_path, entry)

        file_name = data.get('file_name', '')
        error_idx = int(data.get('error_idx', 0))
        for r in results:
            if r['file'] == file_name:
                top_errors = r.get('top_errors', [])
                if 0 <= error_idx < len(top_errors):
                    cur_match = top_errors[error_idx].get('match', {})
                    if cur_match.get('status') == 'matched':
                        # 补充录入：在原有 entries 基础上追加新条目，不覆盖已有匹配
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
                # 重新计算汇总状态
                if not top_errors:
                    r['match'] = {'status': 'no_error', 'entry': None, 'entries': []}
                elif any(e['match']['status'] == 'unmatched' for e in top_errors):
                    r['match'] = {'status': 'unmatched', 'entry': None, 'entries': []}
                else:
                    r['match'] = top_errors[0]['match']
                break

        _set_results(sid, results, db_path)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/query', methods=['POST'])
def query_kb():
    """知识库模糊查询：按错误类型、错误ID（部分匹配）、关键词（任意词命中）搜索。"""
    data     = request.get_json() or {}
    db_path  = data.get('db_path', '').strip() or DB_DEFAULT
    level    = data.get('level',    '').strip().upper()
    error_id = data.get('error_id', '').strip().lower()
    text     = data.get('text',     '').strip().lower()

    try:
        from core.db_manager import load_db
        db_entries = load_db(db_path)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    tokens = [t for t in re.split(r'[\s,，]+', text) if t] if text else []

    SEARCH_FIELDS = ['错误ID', '关键描述关键词', '报错原因', '解决方案', '所属模块', '根因分类']
    scored = []
    for entry in db_entries:
        # 错误类型精确过滤
        if level and str(entry.get('错误类型', '')).strip().upper() != level:
            continue
        # 错误ID部分匹配过滤
        if error_id and error_id not in str(entry.get('错误ID', '')).strip().lower():
            continue
        # 关键词模糊评分：任意词命中即计入
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


@app.route('/export/excel')
def export_excel():
    sid = _sid()
    results, _ = _get_results(sid)
    if not results:
        return '无分析结果', 400
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    fname = 'triage_report_{}.xlsx'.format(ts)
    out_path = str(REPORT_DIR / fname)
    generate_excel(results, out_path)
    return send_file(out_path, as_attachment=True,
                     download_name=fname)


@app.route('/export/html')
def export_html():
    sid = _sid()
    results, _ = _get_results(sid)
    if not results:
        return '无分析结果', 400
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    fname = 'triage_report_{}.html'.format(ts)
    out_path = str(REPORT_DIR / fname)
    generate_html(results, out_path)
    return send_file(out_path, as_attachment=True,
                     download_name=fname)


if __name__ == '__main__':
    import argparse
    import threading
    import webbrowser

    parser = argparse.ArgumentParser(description='simulation log triage tool')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=5000)
    args, _ = parser.parse_known_args()

    url = 'http://{}:{}'.format(args.host, args.port)
    print('启动成功，请浏览器访问: {}'.format(url))

    def _open_browser():
        try:
            webbrowser.open(url)
        except Exception:
            pass

    threading.Timer(1.0, _open_browser).start()
    app.run(host=args.host, port=args.port, debug=False)
