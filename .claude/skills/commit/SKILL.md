---
name: commit
description: Use when the user wants to commit / 提交 / push / "上传到 git" in this triage_tool repo. Audits `git status` and classifies each new or modified path into one of four buckets (track / gitignore / SENSITIVE / ask), updates `.gitignore` for the runtime/build artifacts this project generates, then drafts a commit. Skips the audit if the working tree is clean.
---

# commit

Audit-then-commit workflow tailored to this repo. The triage tool generates a lot of runtime state next to the source (`uploads/`, `reports/`, `dist/error_db.bak*.xlsx`, `.secret_key`, …) and at least one tracked file (`llm_config.json`) can hold a real API key. A naive `git add -A` will leak secrets or pollute the history. Always audit first.

## Procedure

### 1. Snapshot the working tree

Run **in parallel**:
- `git status --short`
- `git diff --stat` (modified-file size sense check)
- `git ls-files log_analysis/triage_tool/llm_config.json` (is the secret-bearing file currently tracked?)

If `git status` is clean, tell the user there's nothing to commit and stop.

### 2. Classify every path from `git status`

For each `??` (untracked) or ` M`/`A`/`D` entry, put it in exactly one bucket:

**TRACK** — source, docs, tests, configs the team needs:
- `*.py`, `templates/**`, `static/**`, `tests/**`
- `requirements.txt`, `pytest.ini`, `triage_tool.spec`, `install_packages.py`
- `*.md` (PRD, BUGLOG, CLAUDE, LLM_*, README, 代码审查报告_*.md, GitHub使用指南.md)
- `error_db.xlsx` (the seed KB, intentionally committed)
- `extra_patterns.json`, `pass_patterns.json` (default config; commit when *intentionally* edited, ignore noise edits)
- `packages/*.whl` (offline wheels — committed on purpose for intranet deploy)
- `.gitignore` itself

**GITIGNORE** — runtime / build artifacts that regenerate on every run:
- `__pycache__/`, `*.pyc`, `*.so`, `build/`, `.pytest_cache/`, `.coverage`, `htmlcov/` (most already covered)
- `log_analysis/triage_tool/uploads/`, `…/reports/`, `…/error_db.xlsx.lock`
- `log_analysis/triage_tool/dist/uploads/`, `…/dist/reports/`, `…/dist/error_db.xlsx.lock`
- `log_analysis/triage_tool/dist/error_db.bak*.xlsx` (rolling backups — `BACKUP_COUNT` rotates them)
- `log_analysis/triage_tool/dist/.secret_key`, `log_analysis/triage_tool/.secret_key` (auto-generated per `app.py`)
- `*.log` (already covered — catches `sim.log`, the 12 MB sample)
- `testlist.txt` (ad-hoc local list, not part of the suite)
- IDE/OS noise: `.vscode/`, `.idea/`, `*.swp`, `.DS_Store`, `Thumbs.db`, `desktop.ini`

The `dist/triage_tool.exe` / `dist/triage_tool` binary is **intentionally NOT ignored** — see the comment at the top of the existing `.gitignore`. Don't add `dist/` wholesale.

**SENSITIVE** — stop and warn the user, do not stage:
- `log_analysis/triage_tool/llm_config.json` and `log_analysis/triage_tool/dist/llm_config.json` — `api_key` field can be a real key. CLAUDE.md says "treat as sensitive; in production prefer env vars."
  - If currently tracked: tell the user, suggest `git rm --cached <path>` + add to `.gitignore` + rotate the key. Do **not** run `git rm` without explicit approval.
  - If untracked: just add to `.gitignore`.
- Any `.env`, `*credentials*`, `*.pem`, `*.key`, `id_rsa*`.

**ASK** — anything you can't confidently bucket (new top-level files, large binaries > 1 MB that aren't `.xlsx`/`.exe`, screenshots that may or may not be doc assets, e.g. `ScreenShot_*.png`). Do not guess.

### 3. Present the plan

Output one compact table grouped by bucket. For each gitignore entry, show the **pattern** you'll write (prefer directory globs over per-file lines), and which existing rule already covers it (skip those — don't write duplicates). For SENSITIVE entries, lead with a one-line warning.

Then ask the user to confirm. Do not edit `.gitignore` or stage anything before they say yes.

### 4. Apply

After confirmation, in parallel where possible:
- Append new patterns to `.gitignore` under the appropriate existing section header (`# ── Application runtime files`, etc.). Use `Edit`, not `Write` — preserve the existing header structure.
- For SENSITIVE files the user agrees to untrack: run `git rm --cached <path>` (one path at a time, never `-r .`).
- `git add` only the TRACK paths, by name. **Never** `git add -A` or `git add .` here — too easy to sweep up `llm_config.json` or a stray `.bak` file.

### 5. Commit

Hand off to the standard commit flow in the system prompt (draft a Why-focused message, use a HEREDOC, include the `Co-Authored-By` trailer). Do not push unless the user asks.

## Guardrails

- Never commit `llm_config.json` with a non-empty `api_key`. If the user insists, refuse and explain.
- Never run `git rm --cached` without explicit per-path approval — it changes index state in a way casual users misread as "deleted my file."
- Never use `--no-verify` or `--amend` on this skill's path. Pre-commit hooks (if added later) catch leaked keys; amending hides the audit trail.
- If a file is *both* changed (` M`) and matches a gitignore-bucket rule, that's a signal the rule was added late — flag it, don't silently `git rm --cached`.
