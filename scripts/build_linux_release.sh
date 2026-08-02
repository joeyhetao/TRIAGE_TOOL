#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ ! -x "$ROOT_DIR/bin/xlog" ] || [ ! -f "$ROOT_DIR/src/xlog/cli.py" ]; then
  echo "[ERROR] Cannot find xlog source" >&2
  exit 1
fi

VERSION="${1:-}"
if [ -z "$VERSION" ]; then
  if command -v git >/dev/null 2>&1 && (cd "$ROOT_DIR" && git rev-parse --is-inside-work-tree >/dev/null 2>&1); then
    VERSION="$(date +%Y%m%d)-$(cd "$ROOT_DIR" && git rev-parse --short HEAD)"
  else
    VERSION="$(date +%Y%m%d)"
  fi
fi

OUT_DIR="$ROOT_DIR/release"
STAGE="$(mktemp -d)"
PKG_ROOT="$STAGE/XLOG"
OUT_FILE="$OUT_DIR/XLOG-linux-$VERSION.tar.gz"
OUT_TMP="$OUT_FILE.tmp.$$"

cleanup() {
  rm -rf "$STAGE"
  rm -f "$OUT_TMP"
}
trap cleanup EXIT

mkdir -p "$PKG_ROOT" "$OUT_DIR"
cp "$ROOT_DIR/README.md" "$ROOT_DIR/PLAN.md" "$PKG_ROOT/"
cp -a "$ROOT_DIR/bin" "$ROOT_DIR/src" "$ROOT_DIR/schemas" "$ROOT_DIR/config" "$PKG_ROOT/"

find "$PKG_ROOT" \( -name '__pycache__' -o -name '.pytest_cache' \) -type d -prune -exec rm -rf {} +
find "$PKG_ROOT" \( -name '*.pyc' -o -name '*.pyo' -o -name '*.log' -o -name '*.tmp' \) -type f -delete

rm -f "$OUT_FILE" "$OUT_TMP"
tar -C "$STAGE" -czf "$OUT_TMP" XLOG
mv "$OUT_TMP" "$OUT_FILE"
find "$OUT_DIR" -maxdepth 1 -type f -name 'XLOG-linux-*.tar.gz' ! -name "$(basename "$OUT_FILE")" -delete

SIZE="$(du -h "$OUT_FILE" | awk '{print $1}')"
echo "[OK] Built $OUT_FILE ($SIZE)"
