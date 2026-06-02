# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository Layout

The application lives entirely under [log_analysis/triage_tool/](log_analysis/triage_tool/). All paths below are relative to that directory unless stated otherwise.

```
log_analysis/triage_tool/
├── app.py              # Flask app factory: llm_client.init, blueprint registration, root route (~100 lines)
├── state.py            # Shared module-level state (_store, _jobs, EXTRA/PASS_PATTERNS, helpers, paths)
├── blueprints/
│   ├── analysis.py     # /analyze, /progress (SSE), /progress_status, /result, /errors, /view_log
│   ├── writeback.py    # /writeback (append KB + update in-memory result)
│   ├── kb.py           # /query, /kb/add, /kb/update, /kb/delete (+ undo)
│   ├── config_bp.py    # /extra_patterns/*, /pass_patterns/* (live-reload)
│   ├── export.py       # /export/excel, /export/html
│   └── llm_bp.py       # 18 LLM routes (P0–P6 + 4 multi-profile routes), independent of analyze/result flow
├── core/
│   ├── log_parser.py   # UVM streaming parser + ThreadPoolExecutor dispatch
│   ├── matcher.py      # Two-stage KB matching per top_errors entry; exposes score_query
│   ├── db_manager.py   # Excel KB I/O with threading.Lock + cross-process _FileLock
│   ├── reporter.py     # Excel + self-contained HTML report generation
│   ├── llm_client.py   # Pure-stdlib (urllib) LLM client; OpenAI/Anthropic auto-detect, cache, hot-reload, multi-profile
│   ├── kb_stats.py     # Activity-scoring layer; appends kb_hits.jsonl, mtime-cached aggregate
│   ├── kb_migrate.py   # Lazy 稳定ID backfill triggered inside load_db
│   └── file_picker.py  # tkinter native file dialog — run as subprocess to avoid Flask thread hang
├── tests/              # pytest (parser/matcher/db/api + LLM: test_llm_prescan, test_llm_profiles, test_kb_stats)
├── templates/          # Jinja2 templates (index.html, result.html, errors.html)
├── static/style.css    # All UI styling
├── packages/           # Offline wheels for intranet deployment
├── requirements.txt    # flask>=2.0, openpyxl>=3.0
├── install_packages.py # Offline installer helper for Linux intranet deployment
├── triage_tool.spec    # PyInstaller build spec (auto-generated, do not edit)
├── error_db.xlsx       # Default knowledge base
├── extra_patterns.json # Runtime config: extra error keywords
├── pass_patterns.json  # Runtime config: pass marker strings
└── llm_config.json     # Runtime LLM config (endpoint, api_key, model, …); env vars override
```

## Running, Testing, and Building

```bash
# Run from source (inside log_analysis/triage_tool/)
pip install -r requirements.txt
python app.py                         # http://127.0.0.1:5000
python app.py --host 0.0.0.0 --port 8080

# Tests (pytest.ini sets testpaths=tests)
pytest                                # full suite
pytest tests/test_matcher.py          # one file
pytest tests/test_matcher.py::test_exact_id_match    # one test
# LLM-layer tests: test_llm_prescan (P2 sliding-window prescan), test_llm_profiles (multi-profile CRUD),
# test_kb_stats (activity scoring). Network calls to real LLM endpoints are still manual via /llm/test_connection.

# Build executable (Windows separator: ;  Linux separator: :)
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
Deploy by copying only the binary; `uploads/`, `reports/`, `error_db.xlsx`, and `llm_config.json` are created/used next to it.

## Intranet / Offline Dependency Constraint

The target environment has **no PyPI access**. Pre-downloaded wheels are in `packages/`. To update:

```bash
pip download flask openpyxl -d ./packages          # internet machine
pip install --no-index --find-links=./packages flask openpyxl   # intranet machine
```

Do not introduce new third-party dependencies. `core/log_parser.py`, `core/matcher.py`, and `core/llm_client.py` are intentionally stdlib-only (no `requests`). On Linux intranet machines, use `install_packages.py`.

**注意**：`install_packages.py` 现在同时处理 pip wheels (`*.whl`) 和系统包 (`*.deb` / `*.rpm`，主要是 `python3-tk` 提供 tkinter)。**有 `.deb`/`.rpm` 时必须 `sudo` 运行**，因为系统包要写 `/usr/lib`。tkinter 仅"选择文件"按钮的原生弹窗用——缺失时自动 fallback 到提示用户手输绝对路径，不阻塞核心功能。完整内网部署流程见 [log_analysis/triage_tool/DEPLOYMENT.md](log_analysis/triage_tool/DEPLOYMENT.md)。

## Architecture

This is a Flask web app for triaging UVM simulation log files against an Excel knowledge base, with an optional LLM enhancement layer.

### Blueprint architecture and the `state.py` rule

`app.py` is the thin entry point: it creates the Flask app, calls `llm_client.init(state.BASE_DIR)` (load `llm_config.json` + env overrides), exposes `llm_enabled` as a Jinja global, persists a random `.secret_key`, registers six blueprints, mounts `/`, and runs `state._cleanup_old_files()`. Routes live in `blueprints/` — find a route by feature area, not by `app.py`.

All shared mutable state lives in [log_analysis/triage_tool/state.py](log_analysis/triage_tool/state.py): `_store`, `_jobs`, `EXTRA_PATTERNS`, `PASS_PATTERNS`, locks, path constants, and helpers (`_sid`, `_get_results`, `_set_results`, `_get_file_paths`, `_get_p3_history`, `_set_p3_history`, `_valid_levels`, `_validate_db_path`, `_conflict_summary`, `_unique_error_counts`, `_push/_pop_delete_undo`).

**Critical convention:** blueprints **must** access shared lists via attribute lookup — `state.EXTRA_PATTERNS`, `state.PASS_PATTERNS`. Never `from state import EXTRA_PATTERNS` — `from`-import binds a local name to the *current list object*, so when `config_bp` rebuilds the list, other blueprints will silently keep using the stale one. This is documented at the top of `state.py`.

### Request flow (analyze/result)

1. User uploads `.log` files **or** specifies server-local glob patterns → `/analyze` (returns `{job_id}` immediately; analysis runs in a background `threading.Thread`)
2. Frontend polls progress via SSE (`/progress/<job_id>`); path-mode glob expansion happens inside the background thread (`phase='scanning'`), not in the request handler. On SSE `onerror`, frontend falls back to `/progress_status/<job_id>` (single JSON poll) to avoid Linux TCP race.
3. `core/log_parser.py` streams each file line-by-line (constant memory, per-file limit 10 GB), extracts up to `TOP_N=5` error entries with up to 3 continuation lines each; files are parsed in parallel via `ThreadPoolExecutor`. Recognises:
   - **UVM pattern** (priority): `UVM_FATAL/ERROR/WARNING` with `@ time: component [ID] msg` format
   - **Generic keyword pattern** (fallback): `^KEYWORD: msg` for any keyword in `EXTRA_PATTERNS`
   - `UVM_WARNING` and WARNING-suffix extra keywords are counted but not entered into `top_errors`
   - **Pass/fail logic**: `PASS` = zero non-WARNING errors **AND** any `PASS_PATTERNS` string found; empty `pass_patterns.json` falls back to old logic (pass = zero errors only).
4. `core/matcher.py` runs two-stage KB matching on **each** of the `top_errors` entries: (1) exact error ID + type, (2) all keywords present in description (AND logic); Chinese full-width comma `，` treated as `,`. `score_query()` is reused by `/llm/similar_errors` for token-overlap pre-filtering.
5. Results rendered in `result.html`: **FAIL files first** (always expanded), **PASS files collapsed** under a "▶ PASS (N)" group header.
6. Reports exported via `/export/excel` and `/export/html`.

### KB management routes (`blueprints/kb.py`)

- `POST /query` — fuzzy search KB: exact `level` filter, partial `error_id` match, any-token scoring across 6 fields; returns top 100
- `POST /kb/add`, `/kb/update`, `/kb/delete` — direct KB row edits; deletions push to `state._undo_buffers[sid]` (max 10/session)

All write endpoints share the duplicate detection flow: `find_duplicates` → return `{duplicate: true, conflicts: [...]}` → frontend confirms → re-call with `force: true`. Dedup rules (any one match triggers conflict, `录入人` excluded): same `错误类型` + `错误ID`; same `错误类型` + `关键描述关键词`; same `错误类型` + `报错原因`; same `错误类型` + `解决方案`.

### Parse-config routes (`blueprints/config_bp.py`)

Live-reload globals, no restart needed:
- `GET/POST /extra_patterns`, `/extra_patterns/add|delete|update`
- `GET/POST /pass_patterns`, `/pass_patterns/add|delete|update`
- `state._valid_levels()` is called dynamically so `/writeback` and `/kb/add` automatically accept newly added extra keywords as valid `错误类型`

### Dual input modes

- **Upload mode**: files saved to `uploads/` with session-prefixed names, deleted immediately after parsing
- **Path mode**: glob expansion runs **inside the background thread** (job starts with `phase='scanning'`); supports `**` recursion, comma/newline-separated patterns, max 5000 files per request, `.log` extension filter. Custom KB paths must pass `state._validate_db_path()` — absolute path, `.xlsx` suffix — to prevent path traversal.

### Session and job state

Both live in `state.py`, module-level:

- `_store[sid] = {'results', 'db_path', 'file_paths', 'p3_history', 'p3_tokens', 'ts'}` — TTL 2 h. `file_paths` populated only in path mode (used by `/llm/custom_extract` for single-file LLM Q&A). `p3_history` (legacy key name; corresponds to current P2 feature) stores the multi-turn LLM conversation, accessed via `state._get_p3_history` / `_set_p3_history`. State is lost on restart by design.
- `_jobs[job_id]` — analyze background jobs. Phase sequence: `scanning` → `parsing` → `matching` → `done|error`. TTL 1 h. SSE generator adds `sleep(1)` before closing on terminal phases to avoid Linux TCP FIN race. `sid` must be extracted before the thread starts — Flask's session proxy is not thread-safe.

### LLM integration layer (this branch's distinguishing feature)

`core/llm_client.py` is a self-contained, stdlib-only LLM client (no `requests`); `blueprints/llm_bp.py` exposes 18 routes. The whole layer is gated by `llm_client.is_configured()` and degrades silently when not configured.

**Init order in `app.py`** (do not reorder):
```python
import state
from core import llm_client
llm_client.init(state.BASE_DIR)              # loads llm_config.json + env vars
app.jinja_env.globals['llm_enabled'] = llm_client.is_configured()
# … then register blueprints, including llm_bp
```
Templates branch on `llm_enabled` to hide AI buttons in "basic" mode. `/llm/save_config` and `/llm/reload_config` mutate this Jinja global at runtime.

**Config resolution** — `llm_config.json` next to `BASE_DIR`, then env-var overrides (`LLM_ENDPOINT`, `LLM_API_KEY`, `LLM_MODEL`, `LLM_TIMEOUT`). Treat `llm_config.json` as sensitive — it may hold a real API key in dev; in production prefer env vars. Defaults are merged from `llm_client._DEFAULTS` (timeout=30, context_window=100000, p3_max_lines=2500 [legacy key, used by P2], cache_ttl=3600, retry x2, P6 window/step/batch sizes).

**Multi-profile schema** — `llm_config.json` is `{"active_profile": "<name>", "profiles": [{name, endpoint, api_key, model, ...}, ...]}`. Old flat format (with top-level `endpoint`/`model`) is auto-migrated to a single profile on first load and rewritten in place (`_migrate_or_validate` in `llm_client.py`). `_config` exposes the *active* profile already merged with defaults + env overrides; routes and skills should read it via `get_config()` and never touch `_profiles_raw` directly. Profile CRUD goes through `/llm/profile/{add,update,delete,activate}` — `delete` refuses to remove the last profile; `activate` triggers a hot-reload + Jinja `llm_enabled` refresh.

**API format auto-detection** — `'anthropic' in endpoint.lower()` selects the Anthropic format:
- OpenAI: `Authorization: Bearer <key>`, payload includes `temperature`, `messages` carries `system` role, response from `choices[0].message.content`. Endpoints ending in `/v1` get `/chat/completions` auto-appended.
- Anthropic: `x-api-key` + `anthropic-version: 2023-06-01`, **system message must move to top-level `system` field** (422 otherwise) — this transformation happens in `_make_payload`. Endpoints get `/v1/messages` auto-appended. Response from `content[0].text`.

**Proxy bypass** — `_http_post` always uses `urllib.request.ProxyHandler({})`, ignoring `http_proxy`/`https_proxy` env vars (required for offline intranet where the proxy is wrong/unreachable).

**Cache layer** — `call_llm_with_cache` keys on `md5(json(messages))[:16]`, TTL from `cache_ttl` (0 disables). In-memory only, cleared on restart.

**Retry** — `call_llm_verbose` retries `llm_max_retries` times with exponential backoff (`llm_retry_delay * 2^attempt`). All public functions return `''` on failure (never raise) so route handlers can degrade gracefully; use `call_llm_verbose` only when you need the error string (e.g. `/llm/test_connection`).

**The 18 LLM routes** (`blueprints/llm_bp.py`):
- **P0** config: `/llm/reload_config`, `/llm/get_config` (api_key masked), `/llm/save_config`, `/llm/test_connection`, plus profile CRUD `/llm/profile/{add,update,delete,activate}`
- **P1** `/llm/rank_entries` — ranks N candidate KB entries against current error, returns `{ranked, reasons, focus_cases}` (focus_cases ≤ 5)
- **P2** `/llm/custom_extract` — multi-turn log Q&A on the **single file** in `state._store[sid]['file_paths'][0]` (path mode only). Pre-scans the file with `_p3_prescan` (sliding-window over UVM/extra/keyword anchors) to fit `p3_max_lines`; `_apply_token_budget` further trims by `context_window - P3_OVERHEAD_TOKENS`. History capped at first 2 + last 10 messages; `clear=true` resets. Re-anchors when current query keywords are absent from first message. (Internal symbols still use the legacy `p3` prefix.)
- **P3** `/llm/similar_errors` — pre-filters DB to top 50 with `score_query`, then asks LLM for top-K similar (default 5)
- **P4** `/llm/batch_patterns` — dedups across-files by `(level, error_id)`, sends top 20 to LLM, returns 3–7 failure-pattern summaries
- **P5** `/llm/semantic_query` — semantic re-rank of KB candidates; intentionally allows empty result (no padding) so unrelated entries are dropped
- **P6** KB quality review (background job): `/llm/kb_review` (start, returns `job_id`), `/llm/kb_review_status`, `/llm/kb_review_stop`, `/llm/kb_review_export` (Excel with 3 sheets: pairs + full A/B detail), `/llm/merge_suggest` (AI-merge two duplicates)

**P6 background-job state lives in `llm_bp._review_jobs`, NOT `state._jobs`** — the schemas differ (review jobs track `suspect_pairs`, `skipped`, `group`, `stop`). Two modes: `fast` (sliding window of `kb_review_window_size` with `kb_review_step_size`) and `deep` (non-overlapping batches of `kb_review_batch_size`). Pairs deduped by `frozenset({_row_idx_a, _row_idx_b})` across the whole run.

### PyInstaller path handling

`state.py` resolves two distinct directories for frozen vs. source execution:

```python
if getattr(sys, 'frozen', False):
    BASE_DIR    = Path(sys.executable).parent  # writable: uploads/, reports/, error_db.xlsx, llm_config.json
    _BUNDLE_DIR = Path(sys._MEIPASS)           # read-only: templates/, static/
else:
    BASE_DIR = _BUNDLE_DIR = Path(__file__).parent
```

- Use `_BUNDLE_DIR` for files read at startup (templates, static assets)
- Use `BASE_DIR` for files written at runtime (uploads, reports, KB, `extra_patterns.json`, `pass_patterns.json`, `llm_config.json`, `.secret_key`)

### Concurrent write safety

`core/db_manager.py` uses two lock layers:

```
threading.Lock  →  serializes threads within the same process
_FileLock       →  serializes across processes/machines (stdlib only)
                   creates error_db.xlsx.lock via O_CREAT|O_EXCL (atomic on NTFS/ext4)
                   auto-clears stale locks older than 60 s  (uses time.time(), not monotonic)
                   raises TimeoutError after 15 s if lock cannot be acquired
                   os.remove() wrapped in try/except OSError for Windows compatibility
```

`load_db` retries up to 3 times on read failure. Rolling backups kept; count exposed as `core.db_manager.BACKUP_COUNT` (re-exported via `state.BACKUP_COUNT`).

### Knowledge base schema

`error_db.xlsx` columns: `错误类型`, `错误ID`, `关键描述关键词`, `报错原因`, `所属模块`, `根因分类`, `解决方案`, `关联用例`, `录入人`, `录入日期`, `稳定ID`

- `关键描述关键词` is comma-separated (`,` or `，`); ALL keywords must match (AND logic)
- Users can supply a custom path (including UNC network share) via the UI; validated by `state._validate_db_path()`
- `稳定ID`: 12-char sha1 prefix of `(错误类型|错误ID|关键描述关键词|报错原因[:200])`. Used by the activity-scoring layer as a stable key invariant under row-number changes. Old KBs without this column are auto-migrated by `core/kb_migrate.py` (lazily triggered inside `load_db` if any row's `稳定ID` is empty; idempotent).

### Activity scoring layer

The activity-scoring layer ([core/kb_stats.py](log_analysis/triage_tool/core/kb_stats.py)) tracks "which KB entries are still hot" and feeds that signal into ranking. **Decoupled from KB**: events live in `kb_hits.jsonl` next to `error_db.xlsx`, not as Excel columns.

**Data flow:**
```
parse → match_error()                                    [in core/matcher.py]
      → record_event(stable_id, 'match')                 [emits 1 line to kb_hits.jsonl]
      → aggregate_stats() (mtime-cached)                 [scans events, returns per-id stats]
      → activity_boost(stats[sid])                       [min(hit_7d/5, 1.0)]
      → final_rank = relevance × (1 + 0.5 × boost)       [in score_query() / match_error multi-hit sort]
      → entry['_stats'] injected for UI badges           [render-time]
```

**Three event sources** (recorded via `kb_stats.record_event(stable_id, source, db_path)`):
- `match` — emitted in [core/matcher.py run_match](log_analysis/triage_tool/core/matcher.py) for every successful KB hit
- `writeback` — emitted in [blueprints/writeback.py](log_analysis/triage_tool/blueprints/writeback.py) after `/writeback` appends a new entry (strong signal: human confirmed relevance)
- `view` — emitted by `/kb/record_view` in [blueprints/kb.py](log_analysis/triage_tool/blueprints/kb.py); reserved for future UI click-through (route exists, frontend doesn't call it yet)

**Storage**: `kb_hits.jsonl` is append-only newline-delimited JSON `{"id":"abc123","ts":1715000000.0,"src":"match"}`. Use `_FileLock` (same lock primitive as `error_db.xlsx`) but on a separate file. `archive_old_events(cutoff_days=180)` rotates pre-cutoff events to `kb_hits_archive.jsonl`.

**Performance**: `aggregate_stats` is mtime-cached; on hot path it's an O(1) dict return. Cold scan is O(N events) — fine for 100k+ events.

**Degradation**: If `kb_hits.jsonl` is missing or corrupt, `aggregate_stats` returns `{}` and the whole system falls back to baseline (no boost, sort by 录入日期 only). This is the design contract — never break ranking on event-layer failure.

**Stats fields per entry** (`{stable_id: {...}}`): `hit_total`, `hit_7d`, `hit_30d`, `last_hit_ts`, `first_hit_ts`, `last_hit_iso`, `first_hit_iso`, `days_since_last`. Templates use `_stats` (injected by `run_match`/`/query`) to render 🔥 (hit_7d > 0) / 💤 (days_since_last ≥ 90) badges.

**Not yet implemented** (designed for v2): LLM-recommendation acceptance signal, "this isn't right" rejection button, exponential decay, P7-extended auto-archive suggestion, cross-case breadth metrics.

## Key Reference Documents

- [log_analysis/triage_tool/DEPLOYMENT.md](log_analysis/triage_tool/DEPLOYMENT.md) — Intranet 源码模式部署完整流程（联网机准备 packages、tkinter 系统包离线、sudo 运行 install_packages.py、Firefox 验证、敏感文件处理）
- [log_analysis/triage_tool/PRD.md](log_analysis/triage_tool/PRD.md) — Full product requirements; update when adding/changing features
- [log_analysis/triage_tool/BUGLOG.md](log_analysis/triage_tool/BUGLOG.md) — Historical bug fixes with root cause analysis; update when fixing bugs
- [log_analysis/triage_tool/LLM_INTEGRATION_PLAN.md](log_analysis/triage_tool/LLM_INTEGRATION_PLAN.md) — Original design spec for the LLM layer (now implemented on this branch)
- [log_analysis/triage_tool/LLM_USAGE_GUIDE.md](log_analysis/triage_tool/LLM_USAGE_GUIDE.md) — End-user setup and feature reference for the LLM layer (config fields, model presets, P0–P6 walkthroughs)
- [log_analysis/triage_tool/velvety-wishing-dewdrop.md](log_analysis/triage_tool/velvety-wishing-dewdrop.md) — Earlier LLM design draft (v1.0); superseded by `LLM_INTEGRATION_PLAN.md`
