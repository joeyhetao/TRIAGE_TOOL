# -*- coding: utf-8 -*-
import os
import re
import sys
import io
import json
import time
import uuid
import secrets
import getpass
import threading
import glob as _glob
from pathlib import Path
from datetime import datetime
from flask import (Flask, render_template, request,
                   jsonify, send_file, session, Response, redirect)
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
from core.db_manager import append_entry, ensure_db, update_entry, delete_entry, find_duplicates
from core.reporter   import generate_excel, generate_html

# ── 初始化路径 ────────────────────────────────────────────
if getattr(sys, 'frozen', False):
    BASE_DIR    = Path(sys.executable).parent
    _BUNDLE_DIR = Path(sys._MEIPASS)
else:
    BASE_DIR    = Path(__file__).parent
    _BUNDLE_DIR = BASE_DIR

try:
    OS_USERNAME = getpass.getuser()
except Exception:
    OS_USERNAME = ''

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

from urllib.parse import quote as _url_quote
app.jinja_env.filters['urlencode'] = lambda s: _url_quote(str(s), safe='')

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

# ── 后台分析任务状态 ──────────────────────────────────
_JOBS_TTL = 3600         # 任务状态保留 1 小时
_jobs: dict = {}         # job_id -> {phase, parse_done, match_done, total, pct, logs, ...}

_CONFLICT_FIELDS = ['错误类型', '错误ID', '关键描述关键词', '报错原因', '所属模块', '录入日期', '_row_idx']

# ── 额外错误关键词配置 ───────────────────────────────────
_EXTRA_PATTERNS_FILE    = BASE_DIR / 'extra_patterns.json'
_EXTRA_PATTERNS_DEFAULT = ['ERROR', 'FATAL', 'FAILED']
_extra_patterns_lock    = threading.Lock()

def _load_extra_patterns() -> list:
    try:
        if _EXTRA_PATTERNS_FILE.exists():
            data = json.loads(_EXTRA_PATTERNS_FILE.read_text(encoding='utf-8'))
            if isinstance(data, list):
                return [str(x).strip().upper() for x in data if str(x).strip()]
    except Exception:
        pass
    return list(_EXTRA_PATTERNS_DEFAULT)

def _save_extra_patterns(patterns: list):
    _EXTRA_PATTERNS_FILE.write_text(
        json.dumps(patterns, ensure_ascii=False, indent=2),
        encoding='utf-8'
    )

EXTRA_PATTERNS: list = _load_extra_patterns()

# ── 通过标记配置 ─────────────────────────────────────────
_PASS_PATTERNS_FILE    = BASE_DIR / 'pass_patterns.json'
_PASS_PATTERNS_DEFAULT = ['JVP TEST PASSED']
_pass_patterns_lock    = threading.Lock()

def _load_pass_patterns() -> list:
    try:
        if _PASS_PATTERNS_FILE.exists():
            data = json.loads(_PASS_PATTERNS_FILE.read_text(encoding='utf-8'))
            if isinstance(data, list):
                return [str(x).strip() for x in data if str(x).strip()]
    except Exception:
        pass
    return list(_PASS_PATTERNS_DEFAULT)

def _save_pass_patterns(patterns: list):
    _PASS_PATTERNS_FILE.write_text(
        json.dumps(patterns, ensure_ascii=False, indent=2),
        encoding='utf-8'
    )

PASS_PATTERNS: list = _load_pass_patterns()


def _valid_levels() -> set:
    """返回所有合法的 level 值（UVM 三项 + 当前 extra_patterns）。"""
    return {'UVM_FATAL', 'UVM_ERROR', 'UVM_WARNING'} | set(EXTRA_PATTERNS)


def _unique_error_counts(results: list) -> dict:
    """跨所有文件对 all_errors 去重，返回各级别唯一错误数（动态包含 extra_patterns）。"""
    seen   = set()
    counts = {}
    for r in results:
        for err in r.get('all_errors', []):
            lvl = err.get('level', '')
            eid = err.get('error_id', '').lower()
            key = (lvl, eid if eid else err.get('description', '')[:80].lower())
            if key not in seen:
                seen.add(key)
                counts[lvl] = counts.get(lvl, 0) + 1
    # 保证 UVM 三项始终存在（即使为 0）
    for k in ('UVM_FATAL', 'UVM_ERROR', 'UVM_WARNING'):
        counts.setdefault(k, 0)
    return counts


def _conflict_summary(conflicts: list) -> list:
    """返回冲突条目的摘要字段列表，用于前端展示。"""
    return [{f: str(e.get(f, '') or '') for f in _CONFLICT_FIELDS} for e in conflicts]
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


def _run_analysis(job_id: str, sid: str, input_data: list,
                  db_path: str, path_mode: bool) -> None:
    """后台线程：解析日志 → 知识库匹配 → 存储结果，全程更新 _jobs[job_id]。
    path_mode=True 时 input_data 是 glob 模式列表，线程内展开；
    path_mode=False 时 input_data 是已保存的上传文件路径列表。
    """
    job = _jobs[job_id]

    def _ts():
        return datetime.now().strftime('%H:%M:%S')

    # ── 路径模式：在后台线程里展开 glob，避免阻塞请求处理 ──────
    if path_mode:
        job['phase'] = 'scanning'
        job['logs'].append(f"[{_ts()}] 正在扫描文件...")
        saved_paths = []
        not_found   = []
        for pattern in input_data:
            matched   = sorted(_glob.glob(pattern, recursive=True))
            log_files = [str(Path(fp).resolve())
                         for fp in matched
                         if Path(fp).is_file()
                         and Path(fp).suffix.lower() == '.log']
            if log_files:
                saved_paths.extend(log_files)
            else:
                not_found.append(pattern)

        if not saved_paths:
            job['phase'] = 'error'
            job['error'] = '未找到任何 .log 文件' + (
                '：' + '；'.join(not_found) if not_found else '')
            return

        if len(saved_paths) > MAX_PATH_FILES:
            job['phase'] = 'error'
            job['error'] = (f'一次最多分析 {MAX_PATH_FILES} 个文件，'
                            f'当前匹配到 {len(saved_paths)} 个，请缩小范围')
            return

        job['total'] = len(saved_paths)
        job['logs'].append(f"[{_ts()}] 扫描完成，共找到 {len(saved_paths)} 个文件")
    else:
        saved_paths = input_data

    total = len(saved_paths)

    def _parse_cb(filename, result, done, tot):
        s = result['statistics']
        job['parse_done'] = done
        job['pct']        = done * 50 // max(tot, 1)
        stat_str = '  '.join(f"{k}:{v}" for k, v in s.items() if v > 0) or '无报错'
        job['logs'].append(f"[{_ts()}] 已解析: {filename}  ({stat_str})")

    def _match_cb(filename, matched, n_errors, done, tot):
        job['match_done'] = done
        job['pct']        = 50 + done * 50 // max(tot, 1)
        if n_errors == 0:
            status_str = '无报错'
        elif matched == n_errors:
            status_str = f'全部命中 ({n_errors} 条)'
        else:
            status_str = f'命中 {matched}/{n_errors} 条'
        job['logs'].append(f"[{_ts()}] 已匹配: {filename}  ({status_str})")

    try:
        job['phase'] = 'parsing'
        job['logs'].append(f"[{_ts()}] 开始解析 {total} 个日志文件...")
        results = parse_logs(saved_paths, progress_cb=_parse_cb,
                             extra_keywords=EXTRA_PATTERNS,
                             pass_patterns=PASS_PATTERNS)

        # 上传模式：解析后立即删除临时文件
        if not path_mode:
            for fp in saved_paths:
                try:
                    Path(fp).unlink()
                except OSError:
                    pass

        # 还原显示文件名，path_mode 下同时保存完整路径供查看功能使用
        for r in results:
            r['file_path'] = r['file'] if path_mode else ''
            r['file'] = Path(r['file']).name
            sid_prefix = f'{sid}_'
            if r['file'].startswith(sid_prefix):
                r['file'] = r['file'][len(sid_prefix):]

        job['phase'] = 'matching'
        job['logs'].append(f"[{_ts()}] 开始知识库匹配...")
        results = run_match(results, db_path, progress_cb=_match_cb)

        _set_results(sid, results, db_path)
        job['pct']      = 100
        job['logs'].append(f"[{_ts()}] 分析完成，共处理 {total} 个文件")
        job['redirect'] = '/result'   # 必须在 phase='done' 之前赋值，避免 SSE 轮询读到 redirect=None
        job['phase']    = 'done'

    except Exception as e:
        job['phase'] = 'error'
        job['error'] = str(e)
        job['logs'].append(f"[{_ts()}] 分析失败: {e}")


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
    return render_template('index.html', db_default=DB_DEFAULT, os_username=OS_USERNAME)


@app.route('/analyze', methods=['POST'])
def analyze():
    sid = _sid()

    db_path = request.form.get('db_path', '').strip() or DB_DEFAULT
    ensure_db(db_path)

    path_mode = request.form.get('path_mode', '').strip()

    if path_mode:
        # ── 路径模式：glob 展开移至后台线程，这里只做基本格式校验 ──────
        raw = request.form.get('log_paths', '').strip()
        if not raw:
            return jsonify({'error': '请输入日志文件路径'}), 400

        patterns = [p.strip()
                    for line in raw.splitlines()
                    for p in line.split(',')
                    if p.strip()]
        # input_data 传 glob 模式列表，由后台线程展开
        input_data = patterns

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

        input_data = saved_paths

    # 创建后台任务，立即返回 job_id 供前端轮询进度
    # path_mode 时 total=0，后台扫描完成后更新；upload 模式已知 total
    job_id = str(uuid.uuid4())
    _jobs[job_id] = {
        'phase':       'scanning' if path_mode else 'pending',
        'parse_done':  0,
        'match_done':  0,
        'total':       0 if path_mode else len(input_data),
        'pct':         0,
        'logs':        [],
        'redirect':    None,
        'error':       None,
        'ts':          time.time(),
    }
    threading.Thread(
        target=_run_analysis,
        args=(job_id, sid, input_data, db_path, bool(path_mode)),
        daemon=True,
    ).start()
    return jsonify({'job_id': job_id})


@app.route('/progress/<job_id>')
def progress_stream(job_id):
    """SSE 端点：推送后台分析任务进度，直到 done / error。"""
    def generate():
        # 清理过期任务
        now = time.time()
        stale = [k for k, v in list(_jobs.items()) if now - v.get('ts', 0) > _JOBS_TTL]
        for k in stale:
            del _jobs[k]

        while True:
            job = _jobs.get(job_id)
            if not job:
                yield 'data: ' + json.dumps(
                    {'phase': 'error', 'error': '任务不存在或已过期'},
                    ensure_ascii=False) + '\n\n'
                return
            payload = {k: v for k, v in job.items() if k != 'ts'}
            yield 'data: ' + json.dumps(payload, ensure_ascii=False) + '\n\n'
            if job.get('phase') in ('done', 'error'):
                # Linux 上服务端立即关闭连接可能导致客户端先收到 FIN 再收到数据，
                # 延迟 1s 确保最终事件被浏览器接收处理后再关闭连接。
                time.sleep(1)
                return
            time.sleep(0.3)

    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


@app.route('/progress_status/<job_id>')
def progress_status(job_id):
    """单次 JSON 轮询任务最终状态，供 SSE onerror 兜底使用。"""
    job = _jobs.get(job_id)
    if not job:
        return jsonify({'phase': 'error', 'error': '任务不存在或已过期'})
    return jsonify({k: v for k, v in job.items() if k != 'ts'})


@app.route('/result')
def result():
    sid = _sid()
    results, db_path = _get_results(sid)
    unique_counts = _unique_error_counts(results)
    return render_template('result.html', results=results, db_path=db_path,
                           unique_counts=unique_counts, os_username=OS_USERNAME,
                           extra_patterns=EXTRA_PATTERNS)


@app.route('/errors')
def errors_view():
    sid = _sid()
    results, _ = _get_results(sid)
    level = request.args.get('level', '').upper()
    if not level:
        return redirect('/result')

    # 跨文件聚合：相同 (level, error_id) 合并，记录出现的文件列表
    seen = {}
    for r in results:
        for err in r.get('all_errors', []):
            if err.get('level') != level:
                continue
            eid = err.get('error_id', '').strip()
            key = eid.lower() if eid else err.get('description', '')[:80].lower()
            if key not in seen:
                seen[key] = {
                    'error_id':    eid,
                    'description': err.get('description', ''),
                    'location':    err.get('location', ''),
                    'files':       [],
                }
            fname = r.get('file', '')
            if fname not in seen[key]['files']:
                seen[key]['files'].append(fname)

    errors = sorted(seen.values(), key=lambda e: -len(e['files']))
    return render_template('errors.html', errors=errors, level=level)


@app.route('/view_log')
def view_log():
    """在浏览器中查看原始日志文件（仅 path_mode 可用）。
    安全校验：只允许查看本次会话分析结果中出现过的路径。
    """
    sid = _sid()
    results, _ = _get_results(sid)
    req_path = request.args.get('path', '').strip()
    if not req_path:
        return '未指定文件路径', 400
    allowed = {r.get('file_path', '') for r in results} - {''}
    if req_path not in allowed:
        return '无权限访问该文件（仅限本次分析的路径模式日志）', 403
    p = Path(req_path)
    if not p.is_file():
        return '文件不存在或已被移动', 404
    return send_file(str(p), mimetype='text/plain; charset=utf-8')


@app.route('/writeback', methods=['POST'])
def writeback():
    sid = _sid()
    results, db_path = _get_results(sid)
    data = request.get_json()

    # S4: 输入校验
    MAX_LEN = 500
    level = data.get('level', '').strip().upper()
    if level not in _valid_levels():
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
        if not data.get('force'):
            conflicts = find_duplicates(db_path, entry)
            if conflicts:
                return jsonify({'success': False, 'duplicate': True,
                                'conflicts': _conflict_summary(conflicts)})
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


@app.route('/kb/add', methods=['POST'])
def kb_add():
    """直接向知识库追加一条新记录，不依赖会话。
    JSON: {db_path?, 错误类型, 错误ID, 关键描述关键词, 报错原因(必填), 所属模块,
           根因分类(必填), 解决方案, 关联用例, 录入人}
    """
    data = request.get_json() or {}
    db_path = data.get('db_path', '').strip() or DB_DEFAULT
    MAX_LEN = 500

    level = data.get('错误类型', '').strip().upper()
    if level not in _valid_levels():
        valid_str = ' / '.join(sorted(_valid_levels()))
        return jsonify({'success': False, 'error': f'错误类型无效，须为 {valid_str}'}), 400
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
                                'conflicts': _conflict_summary(conflicts)})
        append_entry(db_path, entry)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/kb/update', methods=['POST'])
def kb_update():
    """编辑知识库中指定行。JSON: {row_idx, db_path?, 字段名: 值, ...}"""
    data = request.get_json() or {}
    row_idx = data.get('row_idx')
    if not isinstance(row_idx, int) or row_idx < 2:
        return jsonify({'success': False, 'error': '无效的行号'})
    db_path = data.get('db_path', '').strip() or DB_DEFAULT
    # 允许更新的字段（排除内部字段）
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
                                'conflicts': _conflict_summary(conflicts)})
        update_entry(db_path, row_idx, new_data)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/kb/delete', methods=['POST'])
def kb_delete():
    """删除知识库中指定行。JSON: {row_idx, db_path?}"""
    data = request.get_json() or {}
    row_idx = data.get('row_idx')
    if not isinstance(row_idx, int) or row_idx < 2:
        return jsonify({'success': False, 'error': '无效的行号'})
    db_path = data.get('db_path', '').strip() or DB_DEFAULT
    try:
        delete_entry(db_path, row_idx)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/extra_patterns', methods=['GET'])
def extra_patterns_get():
    """返回当前额外错误关键词列表。"""
    return jsonify({'patterns': EXTRA_PATTERNS})


@app.route('/extra_patterns/add', methods=['POST'])
def extra_patterns_add():
    """添加一个关键词。JSON: {keyword: str}"""
    data = request.get_json() or {}
    kw = data.get('keyword', '').strip().upper()
    if not kw:
        return jsonify({'success': False, 'error': '关键词不能为空'}), 400
    if not re.match(r'^[A-Z0-9_]+$', kw):
        return jsonify({'success': False, 'error': '关键词只能包含大写字母、数字和下划线'}), 400
    with _extra_patterns_lock:
        if kw in EXTRA_PATTERNS:
            return jsonify({'success': False, 'error': '关键词已存在'})
        EXTRA_PATTERNS.append(kw)
        try:
            _save_extra_patterns(EXTRA_PATTERNS)
        except Exception as e:
            EXTRA_PATTERNS.remove(kw)
            return jsonify({'success': False, 'error': str(e)}), 500
    return jsonify({'success': True, 'patterns': EXTRA_PATTERNS})


@app.route('/extra_patterns/delete', methods=['POST'])
def extra_patterns_delete():
    """删除一个关键词。JSON: {keyword: str}"""
    data = request.get_json() or {}
    kw = data.get('keyword', '').strip().upper()
    with _extra_patterns_lock:
        if kw not in EXTRA_PATTERNS:
            return jsonify({'success': False, 'error': '关键词不存在'})
        EXTRA_PATTERNS.remove(kw)
        try:
            _save_extra_patterns(EXTRA_PATTERNS)
        except Exception as e:
            EXTRA_PATTERNS.append(kw)
            return jsonify({'success': False, 'error': str(e)}), 500
    return jsonify({'success': True, 'patterns': EXTRA_PATTERNS})


@app.route('/extra_patterns/update', methods=['POST'])
def extra_patterns_update():
    """重命名一个关键词。JSON: {old: str, new: str}"""
    data = request.get_json() or {}
    old_kw = data.get('old', '').strip().upper()
    new_kw = data.get('new', '').strip().upper()
    if not new_kw:
        return jsonify({'success': False, 'error': '新关键词不能为空'}), 400
    if not re.match(r'^[A-Z0-9_]+$', new_kw):
        return jsonify({'success': False, 'error': '关键词只能包含大写字母、数字和下划线'}), 400
    with _extra_patterns_lock:
        if old_kw not in EXTRA_PATTERNS:
            return jsonify({'success': False, 'error': '原关键词不存在'})
        if new_kw in EXTRA_PATTERNS and new_kw != old_kw:
            return jsonify({'success': False, 'error': '新关键词已存在'})
        idx = EXTRA_PATTERNS.index(old_kw)
        EXTRA_PATTERNS[idx] = new_kw
        try:
            _save_extra_patterns(EXTRA_PATTERNS)
        except Exception as e:
            EXTRA_PATTERNS[idx] = old_kw
            return jsonify({'success': False, 'error': str(e)}), 500
    return jsonify({'success': True, 'patterns': EXTRA_PATTERNS})


@app.route('/pass_patterns', methods=['GET'])
def pass_patterns_get():
    """返回当前通过标记字符串列表。"""
    return jsonify({'patterns': PASS_PATTERNS})


@app.route('/pass_patterns/add', methods=['POST'])
def pass_patterns_add():
    """添加一个通过标记。JSON: {pattern: str}"""
    data = request.get_json() or {}
    pt = data.get('pattern', '').strip()
    if not pt:
        return jsonify({'success': False, 'error': '通过标记不能为空'}), 400
    with _pass_patterns_lock:
        if pt in PASS_PATTERNS:
            return jsonify({'success': False, 'error': '该标记已存在'})
        PASS_PATTERNS.append(pt)
        try:
            _save_pass_patterns(PASS_PATTERNS)
        except Exception as e:
            PASS_PATTERNS.remove(pt)
            return jsonify({'success': False, 'error': str(e)}), 500
    return jsonify({'success': True, 'patterns': PASS_PATTERNS})


@app.route('/pass_patterns/delete', methods=['POST'])
def pass_patterns_delete():
    """删除一个通过标记。JSON: {pattern: str}"""
    data = request.get_json() or {}
    pt = data.get('pattern', '').strip()
    with _pass_patterns_lock:
        if pt not in PASS_PATTERNS:
            return jsonify({'success': False, 'error': '标记不存在'})
        PASS_PATTERNS.remove(pt)
        try:
            _save_pass_patterns(PASS_PATTERNS)
        except Exception as e:
            PASS_PATTERNS.append(pt)
            return jsonify({'success': False, 'error': str(e)}), 500
    return jsonify({'success': True, 'patterns': PASS_PATTERNS})


@app.route('/pass_patterns/update', methods=['POST'])
def pass_patterns_update():
    """修改一个通过标记。JSON: {old: str, new: str}"""
    data = request.get_json() or {}
    old_pt = data.get('old', '').strip()
    new_pt = data.get('new', '').strip()
    if not new_pt:
        return jsonify({'success': False, 'error': '新标记不能为空'}), 400
    with _pass_patterns_lock:
        if old_pt not in PASS_PATTERNS:
            return jsonify({'success': False, 'error': '原标记不存在'})
        if new_pt in PASS_PATTERNS and new_pt != old_pt:
            return jsonify({'success': False, 'error': '新标记已存在'})
        idx = PASS_PATTERNS.index(old_pt)
        PASS_PATTERNS[idx] = new_pt
        try:
            _save_pass_patterns(PASS_PATTERNS)
        except Exception as e:
            PASS_PATTERNS[idx] = old_pt
            return jsonify({'success': False, 'error': str(e)}), 500
    return jsonify({'success': True, 'patterns': PASS_PATTERNS})


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
    try:
        app.run(host=args.host, port=args.port, debug=False)
    except OSError as e:
        if 'Address already in use' in str(e) or getattr(e, 'errno', None) in (98, 10048):
            print('\n[错误] 端口 {} 已被占用，无法启动。'.format(args.port))
            print('\n解决方法：')
            print('  1. 换一个端口启动：')
            print('       python3 app.py --port 8080')
            print('  2. 找出并终止占用进程（Linux）：')
            print('       ss -tlnp | grep :{}'.format(args.port))
            print('       kill -9 <PID>')
            print('  3. 一键释放端口（Linux）：')
            print('       fuser -k {}/tcp'.format(args.port))
            print('  4. 找出并终止占用进程（Windows）：')
            print('       netstat -ano | findstr :{}'.format(args.port))
            print('       taskkill /PID <PID> /F')
            sys.exit(1)
        raise
