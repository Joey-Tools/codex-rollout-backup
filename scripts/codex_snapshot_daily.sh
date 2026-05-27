#!/bin/bash
set -euo pipefail

SNAP_DIR="${CODEX_SNAPSHOT_DIR:-$HOME/OneDrive/Backup/dotfiles/codex/snapshots}"
LOG="$HOME/Library/Logs/codex_snapshot_daily.log"
TMP_OUT=""
PUBLISH_OUT=""
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
MIRROR_SUPPRESSED="$(mktemp "${TMPDIR:-/tmp}/codex-snapshot-suppressed.XXXXXX")"

mkdir -p "$SNAP_DIR" "$(dirname "$LOG")"
# shellcheck source=scripts/codex_rollout_mirror_common.sh
. "$SCRIPT_DIR/codex_rollout_mirror_common.sh"
# shellcheck source=scripts/onedrive_unpin_common.sh
. "$SCRIPT_DIR/onedrive_unpin_common.sh"
DATE="$(date +%Y-%m-%d)"
OUT_BASE="$SNAP_DIR/codex-rollouts-$DATE.tar"
SNAPSHOT_STAGING_DIR="${CODEX_SNAPSHOT_STAGING_DIR:-$CODEX_BACKUP_STATE_ROOT/snapshot-tmp}"

copy_snapshot_to_publish_tmp() {
  local staged_path="$1"
  local publish_tmp="$2"

  rm -f "$publish_tmp" 2>/dev/null || true
  if cp -p "$staged_path" "$publish_tmp"; then
    return 0
  fi

  local status=$?
  rm -f "$publish_tmp" 2>/dev/null || true
  echo "Snapshot publish copy failed: $staged_path -> $publish_tmp" >> "$LOG"
  return "$status"
}

publish_snapshot() {
  local tmp_path="$1"
  local final_path="$2"
  local attempts="${CODEX_SNAPSHOT_PUBLISH_RENAME_ATTEMPTS:-12}"
  local delay_seconds="${CODEX_SNAPSHOT_PUBLISH_RENAME_DELAY_SECONDS:-5}"
  local attempt=1
  local status=1

  if ! [[ "$attempts" =~ ^[0-9]+$ ]] || [ "$attempts" -lt 1 ]; then
    attempts=1
  fi

  while [ "$attempt" -le "$attempts" ]; do
    if mv -f "$tmp_path" "$final_path"; then
      return 0
    else
      status=$?
    fi

    if [ ! -e "$tmp_path" ]; then
      echo "Snapshot publish failed and temp file is missing: $tmp_path -> $final_path" >> "$LOG"
      return "$status"
    fi

    if [ "$attempt" -lt "$attempts" ]; then
      echo "Snapshot publish rename failed (attempt $attempt/$attempts): $tmp_path -> $final_path; retrying in ${delay_seconds}s" >> "$LOG"
      sleep "$delay_seconds"
    fi

    attempt=$((attempt + 1))
  done

  echo "Snapshot publish rename failed after $attempts attempts: $tmp_path -> $final_path; leaving temp file for manual recovery" >> "$LOG"
  return "$status"
}

echo "==== $(date) ====" >> "$LOG"

if have_codex_rollout_source_dirs; then
  sync_codex_rollout_mirror "" "" "$MIRROR_SUPPRESSED"
else
  echo "Source rollout directories missing, snapshotting existing mirror: $CODEX_MIRROR_ROOT" >> "$LOG"
fi

TMP_LIST="$(mktemp)"
trap 'rm -f "$TMP_LIST" "${TMP_OUT:-}" "${PUBLISH_OUT:-}" "$MIRROR_SUPPRESSED"' EXIT

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

mkdir -p "$SNAPSHOT_STAGING_DIR"

if command -v zstd >/dev/null 2>&1; then
  echo "Creating zstd snapshot..." >> "$LOG"
  TMP_OUT="$SNAPSHOT_STAGING_DIR/$(basename "$OUT_BASE.zst").tmp.$$"
  PUBLISH_OUT="$OUT_BASE.zst.tmp.$$"
  (cd "$CODEX_MIRROR_ROOT" && tar -cf - -T "$TMP_LIST") | zstd -q -T0 -f -o "$TMP_OUT"
  if ! copy_snapshot_to_publish_tmp "$TMP_OUT" "$PUBLISH_OUT"; then
    TMP_OUT=""
    exit 1
  fi
  if ! publish_snapshot "$PUBLISH_OUT" "$OUT_BASE.zst"; then
    TMP_OUT=""
    exit 1
  fi
  PUBLISH_OUT=""
  rm -f "$TMP_OUT"
  TMP_OUT=""
  SNAP_FILE="$OUT_BASE.zst"
else
  echo "Creating gzip snapshot..." >> "$LOG"
  TMP_OUT="$SNAPSHOT_STAGING_DIR/$(basename "$OUT_BASE.gz").tmp.$$"
  PUBLISH_OUT="$OUT_BASE.gz.tmp.$$"
  (cd "$CODEX_MIRROR_ROOT" && tar -cf - -T "$TMP_LIST") | gzip -c > "$TMP_OUT"
  if ! copy_snapshot_to_publish_tmp "$TMP_OUT" "$PUBLISH_OUT"; then
    TMP_OUT=""
    exit 1
  fi
  if ! publish_snapshot "$PUBLISH_OUT" "$OUT_BASE.gz"; then
    TMP_OUT=""
    exit 1
  fi
  PUBLISH_OUT=""
  rm -f "$TMP_OUT"
  TMP_OUT=""
  SNAP_FILE="$OUT_BASE.gz"
fi

if supports_onedrive_unpin; then
  unpin_onedrive_copy "$SNAP_FILE"
else
  echo "Skipping OneDrive /unpin: snapshot root is not backed by a real OneDrive CloudStorage symlink or the vendor CLI is unavailable." >> "$LOG"
fi

echo "Snapshot created: $SNAP_FILE" >> "$LOG"
