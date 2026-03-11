# -*- coding: utf-8 -*-
import os
import sys
import io
import json
import uuid
from pathlib import Path
from datetime import datetime
from flask import (Flask, render_template, request,
                   jsonify, send_file, session)

# ── Linux 终端编码修正 ────────────────────────────────────
# Linux 服务器 locale 常为 ASCII，直接 print 中文会 UnicodeEncodeError
# 此处统一强制 stdout/stderr 使用 UTF-8，errors='replace' 保证不崩溃
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

# ── 初始化 ───────────────────────────────────────────────
# 打包为 exe 后 __file__ 指向临时解压目录；BASE_DIR 始终指向 exe 所在目录
if getattr(sys, 'frozen', False):
    BASE_DIR    = Path(sys.executable).parent   # exe 旁边，可读写
    _BUNDLE_DIR = Path(sys._MEIPASS)            # 只读，存放模板/静态文件
else:
    BASE_DIR    = Path(__file__).parent
    _BUNDLE_DIR = BASE_DIR

UPLOAD_DIR = BASE_DIR / 'uploads'
REPORT_DIR = BASE_DIR / 'reports'
DB_DEFAULT = str(BASE_DIR / 'error_db.xlsx')

UPLOAD_DIR.mkdir(exist_ok=True)
REPORT_DIR.mkdir(exist_ok=True)

app = Flask(__name__,
            template_folder=str(_BUNDLE_DIR / 'templates'),
            static_folder=str(_BUNDLE_DIR / 'static'))
app.secret_key = 'triage_tool_2026'

# 内存存储当前分析结果（单用户工具，无需复杂Session）
_current_results = {}   # session_id -> results list
_current_db_path = {}   # session_id -> db_path


def _sid():
    if 'sid' not in session:
        session['sid'] = str(uuid.uuid4())
    return session['sid']


# ── 路由 ─────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html', db_default=DB_DEFAULT)


@app.route('/analyze', methods=['POST'])
def analyze():
    sid = _sid()

    # 获取上传的日志文件
    files = request.files.getlist('logs')
    if not files or all(f.filename == '' for f in files):
        return jsonify({'error': '请至少上传一个日志文件'}), 400

    # 获取知识库路径
    db_path = request.form.get('db_path', '').strip() or DB_DEFAULT
    ensure_db(db_path)

    # 保存上传文件到临时目录
    saved_paths = []
    for f in files:
        if f.filename == '':
            continue
        safe_name = f.filename.replace('..', '_')
        save_path = UPLOAD_DIR / f'{sid}_{safe_name}'
        f.save(str(save_path))
        saved_paths.append(str(save_path))

    if not saved_paths:
        return jsonify({'error': '文件保存失败'}), 500

    # 解析 + 匹配
    results = parse_logs(saved_paths)
    results = run_match(results, db_path)

    # 将文件名还原为原始名（去掉sid前缀）
    for r in results:
        r['file'] = Path(r['file']).name.replace(f'{sid}_', '', 1)

    _current_results[sid] = results
    _current_db_path[sid] = db_path

    return jsonify({'redirect': '/result'})


@app.route('/result')
def result():
    sid = _sid()
    results = _current_results.get(sid, [])
    db_path = _current_db_path.get(sid, DB_DEFAULT)
    return render_template('result.html', results=results, db_path=db_path)


@app.route('/writeback', methods=['POST'])
def writeback():
    sid = _sid()
    db_path = _current_db_path.get(sid, DB_DEFAULT)
    data = request.get_json()
    entry = {
        '错误类型':     data.get('level', ''),
        '错误ID':       data.get('error_id', ''),
        '关键描述关键词': data.get('keywords', ''),
        '报错原因':     data.get('reason', ''),
        '所属模块':     data.get('module', ''),
        '根因分类':     data.get('category', ''),
        '解决方案':     data.get('solution', ''),
        '关联用例':     data.get('related_case', ''),
        '录入人':       data.get('author', ''),
    }
    try:
        append_entry(db_path, entry)
        # 更新当前结果中的匹配状态
        results = _current_results.get(sid, [])
        file_name = data.get('file_name', '')
        for r in results:
            if r['file'] == file_name:
                r['match'] = {
                    'status':   'matched',
                    'match_by': 'manual',
                    'entry':    entry,
                }
        _current_results[sid] = results
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/export/excel')
def export_excel():
    sid = _sid()
    results = _current_results.get(sid, [])
    if not results:
        return '无分析结果', 400
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    fname = 'triage_report_{}.xlsx'.format(ts)
    out_path = str(REPORT_DIR / fname)
    generate_excel(results, out_path)
    return send_file(out_path, as_attachment=True,
                     attachment_filename=fname)


@app.route('/export/html')
def export_html():
    sid = _sid()
    results = _current_results.get(sid, [])
    if not results:
        return '无分析结果', 400
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    fname = 'triage_report_{}.html'.format(ts)
    out_path = str(REPORT_DIR / fname)
    generate_html(results, out_path)
    return send_file(out_path, as_attachment=True,
                     attachment_filename=fname)


if __name__ == '__main__':
    import argparse
    import threading
    import webbrowser

    parser = argparse.ArgumentParser(description='simulation log triage tool')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=5000)
    # 打包为 exe 时 argparse 会收到额外参数，忽略未知参数避免报错
    args, _ = parser.parse_known_args()

    url = 'http://{}:{}'.format(args.host, args.port)
    print('启动成功，请浏览器访问: {}'.format(url))

    # 延迟 1 秒后尝试自动打开浏览器；Linux 服务器无桌面时静默跳过
    def _open_browser():
        try:
            webbrowser.open(url)
        except Exception:
            pass

    threading.Timer(1.0, _open_browser).start()

    app.run(host=args.host, port=args.port, debug=False)
