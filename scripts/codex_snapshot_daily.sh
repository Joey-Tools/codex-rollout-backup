#!/bin/bash
set -euo pipefail

SNAP_DIR="${CODEX_SNAPSHOT_DIR:-$HOME/OneDrive/Backup/dotfiles/codex/snapshots}"
LOG="$HOME/Library/Logs/codex_snapshot_daily.log"
TMP_OUT=""
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
MIRROR_SUPPRESSED="$(mktemp "${TMPDIR:-/tmp}/codex-snapshot-suppressed.XXXXXX")"

mkdir -p "$SNAP_DIR" "$(dirname "$LOG")"
# shellcheck source=scripts/codex_rollout_mirror_common.sh
. "$SCRIPT_DIR/codex_rollout_mirror_common.sh"
# shellcheck source=scripts/onedrive_unpin_common.sh
. "$SCRIPT_DIR/onedrive_unpin_common.sh"
DATE="$(date +%Y-%m-%d)"
OUT_BASE="$SNAP_DIR/codex-rollouts-$DATE.tar"

echo "==== $(date) ====" >> "$LOG"

if have_codex_rollout_source_dirs; then
  sync_codex_rollout_mirror "" "" "$MIRROR_SUPPRESSED"
else
  echo "Source rollout directories missing, snapshotting existing mirror: $CODEX_MIRROR_ROOT" >> "$LOG"
fi

TMP_LIST="$(mktemp)"
trap 'rm -f "$TMP_LIST" "${TMP_OUT:-}" "$MIRROR_SUPPRESSED"' EXIT

find_codex_rollout_mirror_files | \
while IFS= read -r -d '' file; do
  rel="${file#"$CODEX_MIRROR_ROOT"/}"
  if mirror_rel_is_suppressed "$MIRROR_SUPPRESSED" "$rel"; then
    echo "Suppressing snapshot entry for disappeared rollout this pass: $rel" >> "$LOG"
    continue
  fi
  printf '%s\n' "$rel"
done > "$TMP_LIST"

if [ ! -s "$TMP_LIST" ]; then
  echo "No mirrored rollout files found, skipping snapshot." >> "$LOG"
  exit 0
fi

if command -v zstd >/dev/null 2>&1; then
  echo "Creating zstd snapshot..." >> "$LOG"
  TMP_OUT="$OUT_BASE.zst.tmp.$$"
  (cd "$CODEX_MIRROR_ROOT" && tar -cf - -T "$TMP_LIST") | zstd -q -T0 -f -o "$TMP_OUT"
  mv "$TMP_OUT" "$OUT_BASE.zst"
  TMP_OUT=""
  SNAP_FILE="$OUT_BASE.zst"
else
  echo "Creating gzip snapshot..." >> "$LOG"
  TMP_OUT="$OUT_BASE.gz.tmp.$$"
  (cd "$CODEX_MIRROR_ROOT" && tar -cf - -T "$TMP_LIST") | gzip -c > "$TMP_OUT"
  mv "$TMP_OUT" "$OUT_BASE.gz"
  TMP_OUT=""
  SNAP_FILE="$OUT_BASE.gz"
fi

if supports_onedrive_unpin; then
  unpin_onedrive_copy "$SNAP_FILE"
else
  echo "Skipping OneDrive /unpin: snapshot root is not backed by a real OneDrive CloudStorage symlink or the vendor CLI is unavailable." >> "$LOG"
fi

echo "Snapshot created: $SNAP_FILE" >> "$LOG"
