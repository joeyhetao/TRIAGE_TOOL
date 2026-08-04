# -*- coding: utf-8 -*-
import os
import json
import time
import uuid
import threading
import subprocess
import glob as _glob
from pathlib import Path
from datetime import datetime
from flask import (Blueprint, render_template, request,
                   jsonify, Response, redirect, send_file)
from werkzeug.utils import secure_filename

import state
from core.log_parser import parse_logs
from core.matcher    import run_match
from core.db_manager import ensure_db

analysis_bp = Blueprint('analysis', __name__)


def _is_rerun_backup_log(filename: str) -> bool:
    """判断是否为 rerun 生成的备份日志；这类日志不应进入分析。"""
    return Path(filename).name.lower().endswith('_bk.log')


def _run_analysis(job_id: str, sid: str, input_data: list,
                  db_path: str, path_mode: bool) -> None:
    """后台线程：解析日志 → 知识库匹配 → 存储结果，全程更新 state._jobs[job_id]。
    path_mode=True 时 input_data 是 glob 模式列表，线程内展开；
    path_mode=False 时 input_data 是已保存的上传文件路径列表。
    """
    job = state._jobs[job_id]

    def _ts():
        return datetime.now().strftime('%H:%M:%S')

    # ── 路径模式：在后台线程里展开 glob ───────────────────
    if path_mode:
        job['phase'] = 'scanning'
        job['logs'].append(f"[{_ts()}] 正在扫描文件...")
        saved_paths = []
        not_found   = []
        skipped_backups = []
        for pattern in input_data:
            matched   = sorted(_glob.glob(pattern, recursive=True))
            log_files = []
            for fp in matched:
                path = Path(fp)
                if not path.is_file() or path.suffix.lower() != '.log':
                    continue
                if _is_rerun_backup_log(path.name):
                    skipped_backups.append(str(path))
                    continue
                log_files.append(str(path.resolve()))
            if log_files:
                saved_paths.extend(log_files)
            else:
                not_found.append(pattern)

        if skipped_backups:
            job['logs'].append(
                f"[{_ts()}] 已剔除 {len(skipped_backups)} 个 rerun 备份日志（*_bk.log）")

        if not saved_paths:
            job['phase'] = 'error'
            job['error'] = '未找到任何 .log 文件' + (
                '：' + '；'.join(not_found) if not_found else '')
            return

        if len(saved_paths) > state.MAX_PATH_FILES:
            job['phase'] = 'error'
            job['error'] = (f'一次最多分析 {state.MAX_PATH_FILES} 个文件，'
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
                             extra_keywords=state.EXTRA_PATTERNS,
                             pass_patterns=state.PASS_PATTERNS)

        # 注意：上传文件不再"解析完即删"——保留以支持 P2 AI 日志问答
        # 多余文件由 state._cleanup_old_files()（启动时扫 uploads/）24h 后清理
        # 会话级 _store TTL 是 2h，过期后用户已经看不到结果页

        # 还原显示文件名，path_mode 下同时保存完整路径供查看功能使用
        for r in results:
            r['file_path'] = r.get('filepath', '') if path_mode else ''
            r['file'] = Path(r['file']).name
            sid_prefix = f'{sid}_'
            if r['file'].startswith(sid_prefix):
                r['file'] = r['file'][len(sid_prefix):]

        job['phase'] = 'matching'
        job['logs'].append(f"[{_ts()}] 开始知识库匹配...")
        results = run_match(results, db_path, progress_cb=_match_cb)

        # 两种模式都填 file_paths——上传模式现在保留文件，让 P2 能拿到
        file_paths = saved_paths
        state._set_results(sid, results, db_path, file_paths=file_paths)
        job['pct']      = 100
        job['logs'].append(f"[{_ts()}] 分析完成，共处理 {total} 个文件")
        job['redirect'] = '/result'   # 必须在 phase='done' 之前赋值
        job['phase']    = 'done'

    except Exception as e:
        job['phase'] = 'error'
        job['error'] = str(e)
        job['logs'].append(f"[{_ts()}] 分析失败: {e}")


@analysis_bp.route('/analyze', methods=['POST'])
def analyze():
    sid = state._sid()

    try:
        db_path = state._validate_db_path(request.form.get('db_path', ''))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    ensure_db(db_path)

    path_mode = request.form.get('path_mode', '').strip()

    if path_mode:
        # ── 路径模式：glob 展开移至后台线程 ──────────────
        raw = request.form.get('log_paths', '').strip()
        if not raw:
            return jsonify({'error': '请输入日志文件路径'}), 400
        patterns = [p.strip()
                    for line in raw.splitlines()
                    for p in line.split(',')
                    if p.strip()]
        input_data = patterns

    else:
        # ── 上传模式：保存到 uploads/ ─────────────────────
        files = request.files.getlist('logs')
        if not files or all(f.filename == '' for f in files):
            return jsonify({'error': '请至少上传一个日志文件'}), 400

        saved_paths = []
        skipped_backup_uploads = 0
        for f in files:
            if f.filename == '':
                continue
            if _is_rerun_backup_log(f.filename):
                skipped_backup_uploads += 1
                continue
            f.seek(0, 2)
            file_size = f.tell()
            f.seek(0)
            if file_size > state.MAX_FILE_SIZE:
                return jsonify({'error': f'文件 {f.filename} 超过10GB大小限制'}), 400
            safe_name = secure_filename(f.filename) or f'file_{uuid.uuid4().hex[:8]}.log'
            save_path = state.UPLOAD_DIR / f'{sid}_{safe_name}'
            f.save(str(save_path))
            saved_paths.append(str(save_path))

        if not saved_paths:
            if skipped_backup_uploads:
                return jsonify({'error': '上传文件均为 rerun 备份日志（*_bk.log），已全部剔除'}), 400
            return jsonify({'error': '文件保存失败'}), 500
        input_data = saved_paths

    state._cleanup_jobs()
    job_id = str(uuid.uuid4())
    with state._jobs_lock:
        state._jobs[job_id] = {
            'phase':       'scanning' if path_mode else 'pending',
            'parse_done':  0,
            'match_done':  0,
            'total':       0 if path_mode else len(input_data),
            'pct':         0,
            'logs':        ([f'已剔除 {skipped_backup_uploads} 个 rerun 备份日志（*_bk.log）']
                            if not path_mode and skipped_backup_uploads else []),
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


@analysis_bp.route('/progress/<job_id>')
def progress_stream(job_id):
    """SSE 端点：推送后台分析任务进度，直到 done / error。"""
    def generate():
        state._cleanup_jobs()

        while True:
            job = state._jobs.get(job_id)
            if not job:
                yield 'data: ' + json.dumps(
                    {'phase': 'error', 'error': '任务不存在或已过期'},
                    ensure_ascii=False) + '\n\n'
                return
            payload = {k: v for k, v in job.items() if k != 'ts'}
            yield 'data: ' + json.dumps(payload, ensure_ascii=False) + '\n\n'
            if job.get('phase') in ('done', 'error'):
                time.sleep(1)
                return
            time.sleep(0.3)

    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


@analysis_bp.route('/progress_status/<job_id>')
def progress_status(job_id):
    """单次 JSON 轮询任务状态，供 SSE onerror 兜底使用。"""
    job = state._jobs.get(job_id)
    if not job:
        return jsonify({'phase': 'error', 'error': '任务不存在或已过期'})
    return jsonify({k: v for k, v in job.items() if k != 'ts'})


@analysis_bp.route('/result')
def result():
    sid        = state._sid()
    results, db_path = state._get_results(sid)
    unique_counts    = state._unique_error_counts(results)
    file_paths       = state._get_file_paths(sid)
    # 区分路径模式 vs 上传模式：上传文件总在 UPLOAD_DIR 下，
    # 路径模式是用户的 server-local 路径，不会落到那里。
    upload_prefix    = str(state.UPLOAD_DIR)
    is_path_mode     = bool(file_paths) and not all(
        p.startswith(upload_prefix) for p in file_paths)
    has_files        = bool(file_paths)   # 任一模式只要有文件，P2 即可用
    is_single_file   = len(results) == 1
    is_multi_file    = len(results) > 1
    return render_template('result.html', results=results, db_path=db_path,
                           unique_counts=unique_counts, os_username=state.OS_USERNAME,
                           extra_patterns=state.EXTRA_PATTERNS,
                           is_path_mode=is_path_mode,
                           has_files=has_files,
                           is_single_file=is_single_file,
                           is_multi_file=is_multi_file)


@analysis_bp.route('/errors')
def errors_view():
    sid = state._sid()
    results, _ = state._get_results(sid)
    level = request.args.get('level', '').strip().upper()
    if not level:
        return redirect('/result')

    grouped_errors = state._unique_errors_by_level(results)
    errors = grouped_errors.get(level, [])
    results_missing = not bool(results)
    return render_template('errors.html', errors=errors, level=level,
                           results_missing=results_missing)


@analysis_bp.route('/view_log')
def view_log():
    """在浏览器中查看原始日志文件（仅 path_mode 可用）。"""
    sid = state._sid()
    results, _ = state._get_results(sid)
    req_path = request.args.get('path', '').strip()
    if not req_path:
        return '未指定文件路径', 400
    allowed = {r.get('file_path', '') for r in results} - {''}
    try:
        req_real = str(Path(req_path).resolve())
    except Exception:
        req_real = req_path
    if req_path not in allowed and req_real not in allowed:
        return '无权限访问该文件（仅限本次分析的路径模式日志）', 403
    p = Path(req_real) if req_real in allowed else Path(req_path)
    if not p.is_file():
        return '文件不存在或已被移动', 404
    return send_file(str(p), mimetype='text/plain; charset=utf-8')


@analysis_bp.route('/open_in_editor')
def open_in_editor():
    """用 gvim 打开日志文件（仅 path_mode 可用）。"""
    sid = state._sid()
    results, _ = state._get_results(sid)
    req_path = request.args.get('path', '').strip()
    if not req_path:
        return jsonify({'ok': False, 'error': '未指定文件路径'}), 400
    allowed = {r.get('file_path', '') for r in results} - {''}
    try:
        req_real = str(Path(req_path).resolve())
    except Exception:
        req_real = req_path
    if req_path not in allowed and req_real not in allowed:
        return jsonify({'ok': False,
                        'error': '无权限访问该文件（仅限本次分析的路径模式日志）'}), 403
    p = Path(req_real) if req_real in allowed else Path(req_path)
    if not p.is_file():
        return jsonify({'ok': False, 'error': '文件不存在或已被移动'}), 404
    try:
        env = os.environ.copy()
        proc = subprocess.Popen(['gvim', str(p)], env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.3)
        rc = proc.poll()
        if rc is not None and rc != 0:
            return jsonify({'ok': False,
                            'error': f'gvim 启动后立即退出（退出码 {rc}），'
                                     f'服务器可能无图形界面（DISPLAY 未设置）。'
                                     f'请复制路径后在本机终端手动执行 gvim。',
                            'path': str(p)}), 500
        return jsonify({'ok': True, 'path': str(p)})
    except FileNotFoundError:
        return jsonify({'ok': False, 'error': 'gvim 未安装或不在 PATH 中',
                        'path': str(p)}), 500
    except Exception as e:
        err = str(e)
        if 'DISPLAY' in err or 'display' in err.lower():
            return jsonify({'ok': False,
                            'error': '无法连接显示器，请复制路径后在终端手动执行 gvim',
                            'path': str(p)}), 500
        return jsonify({'ok': False, 'error': err, 'path': str(p)}), 500
