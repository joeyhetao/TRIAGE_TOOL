#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOOL_DIR="$ROOT_DIR/log_analysis/triage_tool"
REMOTE="${GIT_REMOTE:-origin}"
BRANCH="${GIT_BRANCH:-$(cd "$ROOT_DIR" && git rev-parse --abbrev-ref HEAD)}"
COMMIT_MSG="${1:-}"

if [ -z "$COMMIT_MSG" ]; then
  echo 'Usage: bash scripts/publish_git.sh "commit message"' >&2
  exit 2
fi

cd "$ROOT_DIR"

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "[ERROR] Not inside a git repository: $ROOT_DIR" >&2
  exit 1
fi

if [ -z "$(git status --porcelain)" ]; then
  echo "[ERROR] No changes to commit." >&2
  exit 1
fi

echo "[1/5] Compile check"
cd "$TOOL_DIR"
python3 -m py_compile app.py state.py core/*.py blueprints/*.py

echo "[2/5] Test suite"
python3 -m pytest -q -s

cd "$ROOT_DIR"
echo "[3/5] Commit changes"
git add -A
git commit -m "$COMMIT_MSG"
COMMIT="$(git rev-parse --short HEAD)"

echo "[4/5] Build and verify Linux release package"
bash scripts/build_linux_release.sh
ARCHIVE="$(ls -1t release/TRIAGE_TOOL-linux-*.tar.gz | head -1)"
if tar tzf "$ARCHIVE" | grep -E '(^|/)(\.git|tests|dist|__pycache__|\.pytest_cache|uploads|reports)(/|$)|\.exe$|\.log$|\.secret_key$|llm_config\.json$|sim\.log$'; then
  echo "[ERROR] Forbidden content found in release package: $ARCHIVE" >&2
  exit 1
fi
SIZE="$(du -h "$ARCHIVE" | awk '{print $1}')"
echo "[OK] Release package: $ARCHIVE ($SIZE)"

echo "[5/5] Push"
git push "$REMOTE" "$BRANCH"
echo "[OK] Published $COMMIT to $REMOTE/$BRANCH"
echo "[OK] Release package: $ARCHIVE ($SIZE)"
