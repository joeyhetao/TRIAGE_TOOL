# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository Layout

The application lives entirely under `log_analysis/triage_tool/`. All paths below are relative to that directory unless stated otherwise.

```
log_analysis/triage_tool/
├── app.py              # Flask entry point and all routes
├── requirements.txt    # flask>=2.0, openpyxl>=3.0
├── install_packages.py # Offline installer helper for Linux intranet deployment
├── triage_tool.spec    # PyInstaller build spec (auto-generated, do not edit)
├── error_db.xlsx       # Default knowledge base (Excel)
├── core/
│   ├── log_parser.py   # UVM log streaming parser + parallel dispatch
│   ├── matcher.py      # Two-stage KB matching (per top_errors entry)
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

# Build executable (Windows: separator is ;, Linux: separator is :)
pip install pyinstaller
pyinstaller --onefile --console \
  --add-data "templates;templates" \   # Windows
  --add-data "static;static" \
  --name triage_tool app.py            # Output: dist/triage_tool.exe

pyinstaller --onefile --console \
  --add-data "templates:templates" \   # Linux
  --add-data "static:static" \
  --name triage_tool app.py            # Output: dist/triage_tool
```

Before rebuilding on Windows, close any running `triage_tool.exe` — Windows locks the file.
Deploy by copying only the binary; `uploads/`, `reports/`, and `error_db.xlsx` are created automatically next to it on first run.

## Intranet / Offline Dependency Constraint

The target environment has **no PyPI access**. Pre-downloaded wheels are in `packages/`. To update dependencies:

```bash
# On an internet-connected machine
pip download flask openpyxl -d ./packages

# On the intranet machine
pip install --no-index --find-links=./packages flask openpyxl
```

Do not introduce new third-party dependencies. `core/log_parser.py` and `core/matcher.py` are intentionally stdlib-only.

On Linux intranet machines, use `install_packages.py` instead of manual pip commands — it handles pre-checks, installs to `--user`, and verifies install paths.

## Architecture

This is a Flask web app for triaging UVM simulation log files against an Excel knowledge base.

**Request flow:**
1. User uploads `.log` files **or** specifies server-local glob patterns → `/analyze`
2. `core/log_parser.py` streams each file line-by-line (constant memory regardless of file size, per-file limit 10 GB), extracts up to `TOP_N=5` `UVM_FATAL/ERROR/WARNING` entries with up to 3 continuation lines each; files are parsed in parallel via `ThreadPoolExecutor`; scanning continues to end-of-file to produce accurate FATAL/ERROR/WARNING totals
3. `core/matcher.py` runs two-stage KB matching on **each** of the `top_errors` entries: (1) exact error ID + type, (2) all keywords present in description (AND logic); Chinese full-width comma `，` treated same as `,`
4. Results rendered in `result.html` with per-error match panels; unmatched errors can be written back via `/writeback` (requires `error_idx` to target a specific entry)
5. Reports exported as Excel or HTML via `/export/excel` and `/export/html`

**KB management routes** (independent of the analyze/result session flow):
- `POST /query` — fuzzy search KB: exact `level` filter, partial `error_id` match, any-token scoring across 6 fields; returns top 100
- `POST /kb/add` — add a new KB row directly (bypasses session); duplicate-checked via `find_duplicates` unless `force: true`
- `POST /kb/update` — edit a KB row by `row_idx` (Excel row number ≥ 2); allowed fields are the 9 schema columns
- `POST /kb/delete` — delete a KB row by `row_idx`

All write endpoints share the same duplicate detection flow: call `find_duplicates`, return `{duplicate: true, conflicts: [...]}` for the frontend to confirm, then re-call with `force: true` to proceed. Dedup rules (any one match triggers conflict, `录入人` excluded): same `错误类型` + `错误ID`; same `错误类型` + `关键描述关键词`; same `错误类型` + `报错原因`; same `错误类型` + `解决方案`.

**Dual input modes:**
- *Upload mode*: files saved to `uploads/` with session-prefixed names, deleted immediately after parsing (result kept in `_store`)
- *Path mode*: server reads files directly via `glob.glob()` patterns (supports `**` recursion, comma/newline-separated patterns, max 5000 files per request, `.log` extension filter)

**Session state** is stored in module-level dict `_store` keyed by a UUID from the Flask session cookie. Each entry holds `{'results': ..., 'db_path': ..., 'ts': time.time()}`. Entries expire after 2 hours (`_STORE_TTL = 7200`); stale entries are swept on each access. State is lost on restart by design.

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
                   creates error_db.xlsx.lock via O_CREAT|O_EXCL (atomic on NTFS/ext4)
                   auto-clears stale locks older than 60 s  (uses time.time(), not monotonic)
                   raises TimeoutError after 15 s if lock cannot be acquired
                   os.remove() wrapped in try/except OSError for Windows compatibility
```

`load_db` retries up to 3 times on read failure to tolerate brief write windows.

### Knowledge Base Schema

`error_db.xlsx` columns: `错误类型`, `错误ID`, `关键描述关键词`, `报错原因`, `所属模块`, `根因分类`, `解决方案`, `关联用例`, `录入人`, `录入日期`

- `关键描述关键词` is comma-separated (`,` or `，`); ALL keywords must match (AND logic)
- Users can supply a custom path (including UNC network share) via the UI

## Key Reference Documents

- [log_analysis/triage_tool/PRD.md](log_analysis/triage_tool/PRD.md) — Full product requirements; update when adding or changing features
- [log_analysis/triage_tool/BUGLOG.md](log_analysis/triage_tool/BUGLOG.md) — Historical bug fixes with root cause analysis
