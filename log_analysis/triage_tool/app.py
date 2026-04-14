# -*- coding: utf-8 -*-
import os
import sys
import io
import secrets
from urllib.parse import quote as _url_quote
from flask import Flask, render_template

# ── Linux 终端编码修正 ────────────────────────────────────
if hasattr(sys.stdout, 'buffer'):
    sys.stdout = io.TextIOWrapper(
        sys.stdout.buffer, encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'buffer'):
    sys.stderr = io.TextIOWrapper(
        sys.stderr.buffer, encoding='utf-8', errors='replace')

import state
from core import llm_client
llm_client.init(state.BASE_DIR)

# ── Flask 应用 ────────────────────────────────────────────
app = Flask(__name__,
            template_folder=str(state._BUNDLE_DIR / 'templates'),
            static_folder=str(state._BUNDLE_DIR / 'static'))

app.jinja_env.filters['urlencode'] = lambda s: _url_quote(str(s), safe='')
app.jinja_env.globals['llm_enabled'] = llm_client.is_configured()

# S2: 持久化随机 secret_key（重启后 session 仍有效）
_key_file = state.BASE_DIR / '.secret_key'
if _key_file.exists():
    app.secret_key = _key_file.read_bytes()
else:
    _key = secrets.token_bytes(32)
    _key_file.write_bytes(_key)
    if sys.platform != 'win32':
        os.chmod(str(_key_file), 0o600)
    app.secret_key = _key

# ── Blueprint 注册 ────────────────────────────────────────
from blueprints.analysis  import analysis_bp
from blueprints.writeback import writeback_bp
from blueprints.kb        import kb_bp
from blueprints.config_bp import config_bp
from blueprints.export    import export_bp
from blueprints.llm_bp    import llm_bp

app.register_blueprint(analysis_bp)
app.register_blueprint(writeback_bp)
app.register_blueprint(kb_bp)
app.register_blueprint(config_bp)
app.register_blueprint(export_bp)
app.register_blueprint(llm_bp)

# ── 根路由 ────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html',
                           db_default=state.DB_DEFAULT,
                           os_username=state.OS_USERNAME)

# ── F3: 启动时清理 24 小时前的临时文件 ──────────────────
state._cleanup_old_files()


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
