#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOOL_DIR="$ROOT_DIR/log_analysis/triage_tool"

if [ ! -f "$TOOL_DIR/app.py" ]; then
  echo "[ERROR] Cannot find triage tool source at $TOOL_DIR" >&2
  exit 1
fi

VERSION="${1:-}"
if [ -z "$VERSION" ]; then
  if command -v git >/dev/null 2>&1 && git -C "$ROOT_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    VERSION="$(date +%Y%m%d)-$(git -C "$ROOT_DIR" rev-parse --short HEAD)"
  else
    VERSION="$(date +%Y%m%d)"
  fi
fi

OUT_DIR="$ROOT_DIR/release"
STAGE="$(mktemp -d)"
PKG_ROOT="$STAGE/TRIAGE_TOOL"
PKG_TOOL="$PKG_ROOT/log_analysis/triage_tool"
OUT_FILE="$OUT_DIR/TRIAGE_TOOL-linux-$VERSION.tar.gz"

cleanup() {
  rm -rf "$STAGE"
}
trap cleanup EXIT

mkdir -p "$PKG_TOOL" "$OUT_DIR"
cp "$ROOT_DIR/README.md" "$PKG_ROOT/"

for item in   app.py state.py blueprints core templates static packages   requirements.txt install_packages.py error_db.xlsx extra_patterns.json pass_patterns.json   DEPLOYMENT.md LLM_USAGE_GUIDE.md
 do
  if [ ! -e "$TOOL_DIR/$item" ]; then
    echo "[ERROR] Missing required release item: $item" >&2
    exit 1
  fi
  cp -a "$TOOL_DIR/$item" "$PKG_TOOL/"
done

find "$PKG_ROOT"   \( -name '__pycache__' -o -name '.pytest_cache' \) -type d -prune -exec rm -rf {} +
find "$PKG_ROOT"   \( -name '*.pyc' -o -name '*.pyo' -o -name '*.log' -o -name '*.tmp' -o -name '.secret_key' -o -name 'llm_config.json' \)   -type f -delete

rm -f "$OUT_FILE"
tar -C "$STAGE" -czf "$OUT_FILE" TRIAGE_TOOL

SIZE="$(du -h "$OUT_FILE" | awk '{print $1}')"
echo "[OK] Built $OUT_FILE ($SIZE)"
