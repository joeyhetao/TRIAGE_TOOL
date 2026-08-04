#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REMOTE="${1:-origin}"
EXPECTED_BRANCH="${2:-}"

TOP_LEVEL="$(git -C "$ROOT_DIR" rev-parse --show-toplevel 2>/dev/null)" || {
  echo "[ERROR] Not inside a Git repository: $ROOT_DIR" >&2
  exit 1
}
if [ "$TOP_LEVEL" != "$ROOT_DIR" ] || [ "$(basename "$TOP_LEVEL")" != "log_triage_tool" ]; then
  echo "[ERROR] Refusing to operate outside the log_triage_tool repository: $TOP_LEVEL" >&2
  exit 1
fi
if [ ! -f "$ROOT_DIR/log_analysis/triage_tool/app.py" ]; then
  echo "[ERROR] Missing triage tool entry point: log_analysis/triage_tool/app.py" >&2
  exit 1
fi

REMOTE_URL="$(git -C "$ROOT_DIR" remote get-url "$REMOTE" 2>/dev/null)" || {
  echo "[ERROR] Missing Git remote: $REMOTE" >&2
  exit 1
}
case "$REMOTE_URL" in
  *joeyhetao/TRIAGE_TOOL.git) ;;
  *)
    echo "[ERROR] Refusing unexpected remote for triage tool: $REMOTE_URL" >&2
    exit 1
    ;;
esac

BRANCH="$(git -C "$ROOT_DIR" rev-parse --abbrev-ref HEAD)"
if [ "$BRANCH" = "HEAD" ]; then
  echo "[ERROR] Detached HEAD cannot build or publish a release." >&2
  exit 1
fi
if [ -n "$EXPECTED_BRANCH" ] && [ "$BRANCH" != "$EXPECTED_BRANCH" ]; then
  echo "[ERROR] Expected branch $EXPECTED_BRANCH but checked out $BRANCH" >&2
  exit 1
fi

echo "[OK] Triage scope verified: root=$ROOT_DIR branch=$BRANCH remote=$REMOTE_URL"
