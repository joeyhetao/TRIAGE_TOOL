#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REMOTE="${GIT_REMOTE:-origin}"
BRANCH="${GIT_BRANCH:-$(cd "$ROOT_DIR" && git rev-parse --abbrev-ref HEAD)}"
COMMIT_MSG="${PUBLISH_COMMIT_MSG:-${1:-}}"

if [ -z "$COMMIT_MSG" ]; then
  echo 'Usage: bash scripts/publish_git.sh "commit message"
       or: PUBLISH_COMMIT_MSG="commit message" bash scripts/publish_git.sh' >&2
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
python3 -m py_compile src/xlog/*.py

echo "[2/5] Test suite"
PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}" python3 -m pytest -q

echo "[3/5] Build and verify Linux release package"
bash scripts/build_linux_release.sh
ARCHIVE="$(ls -1t release/XLOG-linux-*.tar.gz | head -1)"
if tar tzf "$ARCHIVE" | grep -E '(^|/)(\.git|tests|dist|__pycache__|\.pytest_cache|uploads|reports|blueprints|templates|static)(/|$)|\.exe$|\.log$|\.secret_key$|llm_config\.json$|error_db\.xlsx$|\.whl$'; then
  echo "[ERROR] Forbidden content found in release package: $ARCHIVE" >&2
  exit 1
fi
SIZE="$(du -h "$ARCHIVE" | awk '{print $1}')"
echo "[OK] Release package: $ARCHIVE ($SIZE)"

echo "[4/5] Commit changes and release package"
git add -A
git commit -m "$COMMIT_MSG"
COMMIT="$(git rev-parse --short HEAD)"

echo "[5/5] Push"
git push "$REMOTE" "$BRANCH"
echo "[OK] Published $COMMIT to $REMOTE/$BRANCH"
echo "[OK] Release package: $ARCHIVE ($SIZE)"
