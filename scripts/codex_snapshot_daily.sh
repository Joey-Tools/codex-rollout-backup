#!/bin/bash
set -euo pipefail

# Keep the canonical scheduled checkout free of Python bytecode from this process tree.
export PYTHONDONTWRITEBYTECODE=1

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
if [ -n "${CODEX_SNAPSHOT_DIR:-}" ]; then
  SNAP_DIR="$CODEX_SNAPSHOT_DIR"
else
  SNAP_DIR="$(python3 "$SCRIPT_DIR/codex_snapshot_path.py" "$HOME")"
fi
CODEX_ROOT="${CODEX_ROOT:-$HOME/.codex}"
CODEX_BACKUP_BASE="${CODEX_BACKUP_BASE:-$HOME/.dotfiles/codex-backup}"
CODEX_BACKUP_STATE_ROOT="${CODEX_BACKUP_STATE_ROOT:-$CODEX_BACKUP_BASE/state}"
SNAPSHOT_STAGING_DIR="${CODEX_SNAPSHOT_STAGING_DIR:-$CODEX_BACKUP_STATE_ROOT/snapshot-tmp}"
LOG="$HOME/Library/Logs/codex_snapshot_daily.log"
TMP_OUT=""
PUBLISH_OUT=""
SNAPSHOT_STAGING_IDENTITY=""
SOURCE_DIRS=()

SNAPSHOT_LOCK_HELPER="$SCRIPT_DIR/codex_snapshot_lock.py"
SNAPSHOT_TREE_VALIDATOR="$SCRIPT_DIR/codex_snapshot_validate_trees.py"

validate_snapshot_tree_paths() {
  local validation_error

  if validation_error="$(python3 "$SNAPSHOT_TREE_VALIDATOR" \
    "$CODEX_ROOT" "$SNAP_DIR" "$SNAPSHOT_STAGING_DIR" "$CODEX_BACKUP_STATE_ROOT" 2>&1)"; then
    return 0
  fi

  printf '%s\n' "$validation_error" >&2
  if [ -d "$(dirname "$LOG")" ]; then
    printf '%s\n' "$validation_error" >> "$LOG"
  fi
  return 2
}

# Validate before lock acquisition because the lock itself creates state files.
validate_snapshot_tree_paths

if [ "${1:-}" = "--snapshot-lock-held" ]; then
  shift
  python3 "$SNAPSHOT_LOCK_HELPER" verify \
    --state-root "$CODEX_BACKUP_STATE_ROOT" \
    --fd 9
else
  mkdir -p "$(dirname "$LOG")"
  exec python3 "$SNAPSHOT_LOCK_HELPER" acquire \
    --state-root "$CODEX_BACKUP_STATE_ROOT" \
    --script "$SCRIPT_DIR/codex_snapshot_daily.sh" \
    --log "$LOG" \
    -- "$@"
fi

# shellcheck source=scripts/onedrive_unpin_common.sh
. "$SCRIPT_DIR/onedrive_unpin_common.sh"
trap 'rm -f "${TMP_OUT:-}" "${PUBLISH_OUT:-}"' EXIT
DATE="$(date +%Y-%m-%d)"
OUT_BASE="$SNAP_DIR/codex-rollouts-$DATE.tar"

copy_snapshot_to_publish_tmp() {
  local staged_path="$1"
  local publish_tmp="$2"

  rm -f "$publish_tmp" 2>/dev/null || true
  if cp -p "$staged_path" "$publish_tmp"; then
    return 0
  else
    local status=$?
  fi

  rm -f "$publish_tmp" 2>/dev/null || true
  echo "Snapshot publish copy failed: $staged_path -> $publish_tmp" >> "$LOG"
  return "$status"
}

preserve_staged_snapshot_for_recovery() {
  local staged_path="$1"

  [ -n "$staged_path" ] || return 0
  echo "Preserving staged snapshot for manual recovery: $staged_path" >> "$LOG"
}

snapshot_staging_dir_identity() {
  if [ ! -e "$SNAPSHOT_STAGING_DIR" ] || \
    [ -L "$SNAPSHOT_STAGING_DIR" ] || \
    [ ! -d "$SNAPSHOT_STAGING_DIR" ]; then
    return 1
  fi

  /usr/bin/stat -f '%d:%i' "$SNAPSHOT_STAGING_DIR"
}

report_unsafe_snapshot_staging_dir() {
  printf 'Snapshot staging path is not a non-symlink directory: %s\n' \
    "$SNAPSHOT_STAGING_DIR" >&2
  printf 'Snapshot staging path is not a non-symlink directory: %s\n' \
    "$SNAPSHOT_STAGING_DIR" >> "$LOG"
}

preflight_snapshot_staging_dir() {
  if [ -L "$SNAPSHOT_STAGING_DIR" ] || \
    { [ -e "$SNAPSHOT_STAGING_DIR" ] && [ ! -d "$SNAPSHOT_STAGING_DIR" ]; }; then
    report_unsafe_snapshot_staging_dir
    return 2
  fi
}

initialize_snapshot_staging_dir_identity() {
  if ! SNAPSHOT_STAGING_IDENTITY="$(snapshot_staging_dir_identity)"; then
    report_unsafe_snapshot_staging_dir
    return 2
  fi
}

validate_snapshot_staging_dir_identity() {
  local current_identity

  if ! current_identity="$(snapshot_staging_dir_identity)" || \
    [ "$current_identity" != "$SNAPSHOT_STAGING_IDENTITY" ]; then
    printf 'Snapshot staging directory identity changed: %s\n' \
      "$SNAPSHOT_STAGING_DIR" >&2
    printf 'Snapshot staging directory identity changed: %s\n' \
      "$SNAPSHOT_STAGING_DIR" >> "$LOG"
    return 2
  fi
}

remove_old_zstd_staging_files() {
  validate_snapshot_staging_dir_identity
  find "$SNAPSHOT_STAGING_DIR" \
    -maxdepth 1 \
    -type f \
    -name 'codex-rollouts-*.tar.zst.tmp.*' \
    -delete
}

publish_snapshot() {
  local tmp_path="$1"
  local final_path="$2"
  local attempts="${CODEX_SNAPSHOT_PUBLISH_RENAME_ATTEMPTS:-12}"
  local delay_seconds="${CODEX_SNAPSHOT_PUBLISH_RENAME_DELAY_SECONDS:-5}"
  local attempt=1
  local status=1

  if ! [[ "$attempts" =~ ^[0-9]+$ ]] || [ "${#attempts}" -gt 4 ] || [ "$attempts" -lt 1 ]; then
    attempts=1
  fi
  if ! [[ "$delay_seconds" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
    delay_seconds=5
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

  echo "Snapshot publish rename failed after $attempts attempts: $tmp_path -> $final_path" >> "$LOG"
  return "$status"
}

echo "==== $(date) ====" >> "$LOG"

validate_snapshot_tree_paths
SOURCE_DIRS=()
for source_name in sessions archived_sessions; do
  if [ -d "$CODEX_ROOT/$source_name" ]; then
    SOURCE_DIRS+=("$source_name")
  fi
done
if [ "${#SOURCE_DIRS[@]}" -eq 0 ]; then
  echo "Source rollout directories missing, skipping snapshot." >> "$LOG"
  exit 0
fi

mkdir -p "$SNAP_DIR"
preflight_snapshot_staging_dir
mkdir -p "$SNAPSHOT_STAGING_DIR"
initialize_snapshot_staging_dir_identity
validate_snapshot_tree_paths

if command -v zstd >/dev/null 2>&1; then
  echo "Creating zstd snapshot..." >> "$LOG"
  remove_old_zstd_staging_files
  validate_snapshot_staging_dir_identity
  TMP_OUT="$SNAPSHOT_STAGING_DIR/$(basename "$OUT_BASE.zst").tmp.$$"
  PUBLISH_OUT="$OUT_BASE.zst.publish.tmp.$$"
  tar -cf - -C "$CODEX_ROOT" "${SOURCE_DIRS[@]}" | zstd -q -T0 -f -o "$TMP_OUT"
  if copy_snapshot_to_publish_tmp "$TMP_OUT" "$PUBLISH_OUT"; then
    :
  else
    snapshot_status=$?
    preserve_staged_snapshot_for_recovery "$TMP_OUT"
    TMP_OUT=""
    exit "$snapshot_status"
  fi
  if publish_snapshot "$PUBLISH_OUT" "$OUT_BASE.zst"; then
    :
  else
    snapshot_status=$?
    preserve_staged_snapshot_for_recovery "$TMP_OUT"
    TMP_OUT=""
    exit "$snapshot_status"
  fi
  PUBLISH_OUT=""
  rm -f "$TMP_OUT"
  TMP_OUT=""
  SNAP_FILE="$OUT_BASE.zst"
else
  echo "Creating gzip snapshot..." >> "$LOG"
  validate_snapshot_staging_dir_identity
  TMP_OUT="$SNAPSHOT_STAGING_DIR/$(basename "$OUT_BASE.gz").tmp.$$"
  PUBLISH_OUT="$OUT_BASE.gz.publish.tmp.$$"
  tar -cf - -C "$CODEX_ROOT" "${SOURCE_DIRS[@]}" | gzip -c > "$TMP_OUT"
  if copy_snapshot_to_publish_tmp "$TMP_OUT" "$PUBLISH_OUT"; then
    :
  else
    snapshot_status=$?
    preserve_staged_snapshot_for_recovery "$TMP_OUT"
    TMP_OUT=""
    exit "$snapshot_status"
  fi
  if publish_snapshot "$PUBLISH_OUT" "$OUT_BASE.gz"; then
    :
  else
    snapshot_status=$?
    preserve_staged_snapshot_for_recovery "$TMP_OUT"
    TMP_OUT=""
    exit "$snapshot_status"
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
