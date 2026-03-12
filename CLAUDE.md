# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository Layout

The application lives entirely under `log_analysis/triage_tool/`. All paths below are relative to that directory unless stated otherwise.

```
log_analysis/triage_tool/
├── app.py              # Flask entry point and all routes
├── requirements.txt    # flask>=2.0, openpyxl>=3.0
├── triage_tool.spec    # PyInstaller build spec (auto-generated, do not edit)
├── error_db.xlsx       # Default knowledge base (Excel)
├── core/
│   ├── log_parser.py   # UVM log regex parsing
│   ├── matcher.py      # Two-stage KB matching
│   ├── db_manager.py   # Excel KB read/write with concurrent locking
│   └── reporter.py     # Excel and HTML report generation
├── templates/          # Jinja2 templates (index.html, result.html)
├── static/style.css    # All UI styling
└── packages/           # Offline wheels for intranet deployment
```

## Running and Building

```bash
# Run from source (inside log_analysis/triage_tool/)
pip install -r requirements.txt
python app.py                         # http://127.0.0.1:5000
python app.py --host 0.0.0.0 --port 8080

# Build single Windows executable
pip install pyinstaller
pyinstaller --onefile --console \
  --add-data "templates;templates" \
  --add-data "static;static" \
  --name triage_tool \
  app.py
# Output: dist/triage_tool.exe (~18 MB)
```

Before rebuilding, close any running `triage_tool.exe` — Windows locks the file.
Deploy by copying only `dist/triage_tool.exe`; `uploads/`, `reports/`, and `error_db.xlsx` are created automatically next to the exe on first run.

## Intranet / Offline Dependency Constraint

The target environment has **no PyPI access**. Pre-downloaded wheels are in `packages/`. To update dependencies:

```bash
# On an internet-connected machine
pip download flask openpyxl -d ./packages

# On the intranet machine
pip install --no-index --find-links=./packages flask openpyxl
```

Do not introduce new third-party dependencies. `core/log_parser.py` and `core/matcher.py` are intentionally stdlib-only.

## Architecture

This is a Flask web app for triaging UVM simulation log files against an Excel knowledge base.

**Request flow:**
1. User uploads `.log` files → `/analyze`
2. `core/log_parser.py` extracts `UVM_FATAL/ERROR/WARNING` entries via regex; identifies the highest-priority "first error" per file
3. `core/matcher.py` matches the first error against `error_db.xlsx` in two stages: (1) exact error ID + type, (2) all keywords present in description (AND logic)
4. Results rendered in `result.html`; unmatched errors can be written back via `/writeback`
5. Reports exported as Excel or HTML via `/export/excel` and `/export/html`

**Session state** is stored in module-level dicts (`_current_results`, `_current_db_path`) keyed by a UUID from the Flask session cookie — not in Flask's session object. State is lost on restart by design.

### PyInstaller Path Handling

`app.py` resolves two distinct directories for frozen vs. source execution:

```python
if getattr(sys, 'frozen', False):
    BASE_DIR    = Path(sys.executable).parent  # writable: uploads/, reports/, error_db.xlsx
    _BUNDLE_DIR = Path(sys._MEIPASS)           # read-only: templates/, static/
else:
    BASE_DIR = _BUNDLE_DIR = Path(__file__).parent
```

- Use `_BUNDLE_DIR` for files read at startup (templates, static assets)
- Use `BASE_DIR` for files written at runtime (uploads, reports, KB)

### Concurrent Write Safety

`core/db_manager.py` uses two lock layers to handle simultaneous writes across processes and network drives:

```
threading.Lock  →  serializes threads within the same process
_FileLock       →  serializes across processes/machines (stdlib only)
                   creates error_db.xlsx.lock via O_CREAT|O_EXCL (atomic on NTFS)
                   auto-clears stale locks older than 60 s
                   raises TimeoutError after 15 s if lock cannot be acquired
```

`load_db` retries up to 3 times on read failure to tolerate brief write windows.

### Knowledge Base Schema

`error_db.xlsx` columns: `错误类型`, `错误ID`, `关键描述关键词`, `报错原因`, `所属模块`, `根因分类`, `解决方案`, `关联用例`, `录入人`, `录入日期`

- `关键描述关键词` is comma-separated; ALL keywords must match (AND logic)
- Users can supply a custom path (including UNC network share) via the UI

## Key Reference Documents

- [log_analysis/triage_tool/PRD.md](log_analysis/triage_tool/PRD.md) — Full product requirements; update when adding or changing features
- [log_analysis/triage_tool/BUGLOG.md](log_analysis/triage_tool/BUGLOG.md) — Historical bug fixes with root cause analysis
