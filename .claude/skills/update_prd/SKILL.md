---
name: update_prd
description: Use when the user finishes a feature change in this triage_tool repo and asks to "update PRD" / "更新 PRD" / "更新文档" / "PRD 要不要更新", or after a feature commit when docs are stale. Audits what changed, decides which of PRD.md / LLM_INTEGRATION_PLAN.md / LLM_USAGE_GUIDE.md need to be updated, and drafts the edits. AI-related changes always update all three; non-AI changes only touch PRD.md.
---

# update_prd

Keep the three living docs in lockstep with code changes.

| Doc | Audience | Update when |
|---|---|---|
| `log_analysis/triage_tool/PRD.md` | All feature work | Any user-visible behavior change, new route, new UI element, schema change |
| `log_analysis/triage_tool/LLM_INTEGRATION_PLAN.md` | LLM layer maintainers | Any change touching `core/llm_client.py`, `blueprints/llm_bp.py`, P0–P7 routes, AI buttons in templates, `llm_config.json` schema |
| `log_analysis/triage_tool/LLM_USAGE_GUIDE.md` | End users of AI features | Any change to AI-feature *behavior* a user would notice, or to the config-field set |

Bug fixes go to `BUGLOG.md`, not these docs. Pure refactors with no user-visible change get **no** entry — don't pad changelogs.

## Procedure

### 1. Find what changed

Run **in parallel**:
- `git diff --stat main...HEAD` (or against the baseline the user names)
- `git log --oneline main..HEAD` (commit messages for context)
- `git diff main...HEAD -- log_analysis/triage_tool/blueprints/ log_analysis/triage_tool/core/ log_analysis/triage_tool/templates/ log_analysis/triage_tool/static/` (the actual surface area)

If there are no changes outside the three doc files themselves, tell the user and stop.

### 2. Classify the diff

For each changed file group, decide:

- **AI-related** if the path matches any of: `core/llm_client.py`, `blueprints/llm_bp.py`, `llm_config.json`, or the diff touches an AI button/Tab in `templates/index.html` / `templates/result.html` (search for `llm_`, `ai-`, `🤖`, `P1`–`P7` markers). → **all three docs**
- **Feature change** if it's a new route, new UI control, behavior change, schema field, or constant the user can perceive. → **PRD only**
- **Refactor / internal-only** if no behavior visible to a user changed (e.g. constant moved, helper extracted, comments). → **no doc update**, tell the user why.
- **Mixed**: split into separate changelog entries — one row per behavior change, not one row per commit.

If you can't tell, ask the user one targeted question (e.g. "Does this change anything a user sees, or is it pure refactor?") before drafting.

### 3. Draft the edits — show before applying

Present a plan as a table:

| Doc | Section to edit | New version / date | Summary of edit |

Then for each doc, show the actual proposed text. Do not call `Edit` until the user confirms.

#### PRD.md edit pattern

The doc is structured as: header (version + 基准代码版本 date), §3 当前功能 with subsections `3.1`–`3.N` (each may carry a `(vX.Y 新增)` tag), §4 数据结构, §5 技术约束, §6 接口清单, §7 已知限制, §8 变更记录 (chronological table).

Two edits per change:
1. **Body**: amend or add a `### 3.N` subsection in §3 (or §4/§6 if a schema/route was added). Reuse an existing 3.N if the topic already has one — don't fragment.
2. **Changelog row** in §8. Format must match existing rows exactly:
   ```
   | vX.Y | YYYY-MM-DD | **<feature name>**：<one-paragraph description> | `path/to/file.py`, `path/to/template.html` |
   ```
   - Description should be self-contained — a future maintainer reads only this row.
   - File list uses backtick-quoted relative paths, comma-separated.
   - Date is today's date (Jinja sees `currentDate` in the system context — use that, never invent a date).

3. **Header version + date**: bump `v2.2` → next minor (e.g. `v2.3`) and `基准代码版本: 2026-04-02` → today **only when** at least one substantive feature change is being added. Cosmetic doc edits don't bump the version.

#### LLM_INTEGRATION_PLAN.md edit pattern

The doc has: top version note block (`v4.0`, `v4.1`, `v4.2` …), §一 版本差异对比, §二 核心架构 (2.1 dir / 2.2 app.py / 2.3 llm_client / 2.4 Anthropic / 2.5 config / 2.6 mode), §三 LLM 注入点 P0–P7, §四 工程基础设施, §五 文件变更清单 with 已实现路由汇总, §六 降级与安全.

Per change:
1. **Top version note**: add a new `> **vX.Y**：…` line *below* the existing v4.2 line — do not rewrite the v4.0/4.1/4.2 narrative.
2. **Affected section**: amend the matching deepest section (a new P-feature → §三 P_N; cache/retry/config change → §二 2.5 or §四; new route → §五 路由汇总).
3. **路由汇总**: if the route count changed, update both the "（共 14 条）" header and the route list. The numbers must agree.

#### LLM_USAGE_GUIDE.md edit pattern

User-facing tone — imperative steps, screenshots-style instructions. Sections: 一 快速上手, 二 模式切换, 三 配置字段, 四 各 AI 功能 P0–P7, 五 FAQ, 六 配置示例.

Per change:
1. New config field → add row to §三 table (必填 / 常用可选 / P7 专用 — pick the right table).
2. Behavior change in P_N → amend §四 P_N section. Keep the structure (适用场景 / 操作步骤 / 失败提示) consistent with siblings.
3. New failure mode users hit → consider an §五 FAQ entry.

Do NOT bump LLM_USAGE_GUIDE versions — it has no version header.

### 4. Apply

After the user confirms, use `Edit` (not `Write`) for each doc — these files are large and Write would force a re-read. Edit the smallest unique anchor possible (e.g. the `## 8. 变更记录` table — append a row by anchoring on the last existing row).

Apply all three docs in one batch (parallel `Edit` calls when the anchors are independent), then summarize what was added and where.

## Guardrails

- **Never** invent version numbers — read the current top version, increment by `+0.1` minor (or `+1.0` for a major architectural shift the user explicitly calls out).
- **Never** invent a date — use the date from the system context (`currentDate`).
- **Never** silently rewrite an existing changelog row — if a prior row was wrong, ask the user before editing it; otherwise only append.
- AI changes that update PRD must update **all three** AI docs. If the user explicitly says "PRD only", honor that but warn once that the LLM docs will drift.
- If the diff is large (> 5 logical changes), break the changelog into multiple rows — one per discrete user-visible change. Do not collapse into a single mega-row.
- If a change reverts a prior feature, append a new row that says "revert" rather than deleting the original row.
