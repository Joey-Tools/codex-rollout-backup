#!/bin/bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SNAPSHOT_SCRIPT="$REPO_ROOT/scripts/codex_snapshot_daily.sh"

assert_file_exists() {
  local path="$1"

  if [ ! -f "$path" ]; then
    printf 'Expected file to exist: %s\n' "$path" >&2
    exit 1
  fi
}

assert_contains() {
  local path="$1"
  local needle="$2"

  if ! grep -Fq "$needle" "$path"; then
    printf 'Expected %s to contain: %s\n' "$path" "$needle" >&2
    exit 1
  fi
}

assert_not_contains() {
  local path="$1"
  local needle="$2"

  if grep -Fq "$needle" "$path"; then
    printf 'Did not expect %s to contain: %s\n' "$path" "$needle" >&2
    exit 1
  fi
}

assert_not_exists() {
  local path="$1"

  if [ -e "$path" ]; then
    printf 'Did not expect path to exist: %s\n' "$path" >&2
    exit 1
  fi
}

new_home() {
  local tmp_home

  tmp_home="$(mktemp -d /tmp/codex-scripts-test.XXXXXX)"
  tmp_home="$(cd "$tmp_home" && pwd -P)"
  mkdir -p "$tmp_home/Library/Logs"
  printf '%s\n' "$tmp_home"
}

cleanup_home() {
  local tmp_home="$1"

  rm -rf "$tmp_home"
}

setup_fake_onedrive_cli() {
  local cli_path="$1"

  mkdir -p "$(dirname "$cli_path")"
  cat > "$cli_path" <<'EOF'
#!/bin/bash
set -euo pipefail

state_dir="${TMP_FAKE_ONEDRIVE_STATE:?}"
mkdir -p "$state_dir"

cmd="${1:?}"
path="${2:?}"
count_file="$state_dir/getpin_count"
log_file="$state_dir/cli.log"
unpin_marker="$state_dir/unpinned"

count=0
if [ -f "$count_file" ]; then
  count=$(cat "$count_file")
fi

case "$cmd" in
  /getpin)
    count=$((count + 1))
    printf '%s\n' "$count" > "$count_file"
    printf '%s %s #%s\n' "$cmd" "$path" "$count" >> "$log_file"
    if [ -f "$unpin_marker" ]; then
      printf 'pin state=Unpinned\n'
    elif [ "$count" -eq 1 ]; then
      printf 'Failed operation=3 path=%s recurse=0 status=-2\n' "$path"
    else
      printf 'pin state=None\n'
    fi
    exit 1
    ;;
  /unpin)
    printf '%s %s\n' "$cmd" "$path" >> "$log_file"
    : > "$unpin_marker"
    printf 'unpinned %s\n' "$path"
    exit 1
    ;;
  *)
    printf 'Unsupported fake OneDrive command: %s\n' "$cmd" >&2
    exit 1
    ;;
esac
EOF
  chmod +x "$cli_path"
}

setup_fake_launchd_tools() {
  local bin_dir="$1"
  local log_path="$2"

  mkdir -p "$bin_dir"
  cat > "$bin_dir/launchctl" <<EOF
#!/bin/bash
set -euo pipefail
printf '%s\n' "launchctl \$*" >> "$log_path"
EOF
  cat > "$bin_dir/plutil" <<EOF
#!/bin/bash
set -euo pipefail
if [ "\${1:-}" != "-lint" ] || [ ! -f "\${2:-}" ]; then
  exit 1
fi
printf '%s: OK\n' "\$2"
EOF
  chmod +x "$bin_dir/launchctl" "$bin_dir/plutil"
}

setup_fake_mv_once() {
  local mv_path="$1"

  mkdir -p "$(dirname "$mv_path")"
  cat > "$mv_path" <<'EOF'
#!/bin/bash
set -euo pipefail

state_dir="${TMP_FAKE_MV_STATE:?}"
fail_dst="${TMP_FAKE_MV_FAIL_DST:?}"
log_file="$state_dir/mv.log"
marker="$state_dir/failed-once"
dst="${@: -1}"

mkdir -p "$state_dir"
printf 'mv %s\n' "$*" >> "$log_file"

if [ "$dst" = "$fail_dst" ] && [ ! -e "$marker" ]; then
  : > "$marker"
  printf 'mv: rename %s to %s: Operation not permitted\n' "${*: -2:1}" "$dst" >&2
  exit 1
fi

exec /bin/mv "$@"
EOF
  chmod +x "$mv_path"
}

setup_disappear_hook() {
  local hook_path="$1"

  mkdir -p "$(dirname "$hook_path")"
  cat > "$hook_path" <<'EOF'
#!/bin/bash
set -euo pipefail

state_dir="${TMP_MIRROR_HOOK_STATE:?}"
target_stage="${TMP_MIRROR_HOOK_STAGE:-before_stat}"
target_rel="${TMP_MIRROR_HOOK_TARGET_REL:-sessions/day/rollout-disappear.jsonl}"
stage="${1:?}"
path="${2:?}"
rel="${3:?}"
marker="$state_dir/$stage.$(basename "$path").done"

mkdir -p "$state_dir"

if [ "$stage" = "$target_stage" ] && [ ! -e "$marker" ] && [ "$rel" = "$target_rel" ]; then
  rm -f "$path"
  : > "$marker"
fi
EOF
  chmod +x "$hook_path"
}

snapshot_archive_path() {
  local tmp_home="$1"
  local base_path

  base_path="$tmp_home/OneDrive/Backup/dotfiles/codex/snapshots/codex-rollouts-$(date +%Y-%m-%d).tar"
  if command -v zstd >/dev/null 2>&1; then
    printf '%s.zst\n' "$base_path"
  else
    printf '%s.gz\n' "$base_path"
  fi
}

resolved_snapshot_archive_path() {
  local tmp_home="$1"
  local base_path

  base_path="$tmp_home/Library/CloudStorage/OneDrive/Backup/dotfiles/codex/snapshots/codex-rollouts-$(date +%Y-%m-%d).tar"
  if command -v zstd >/dev/null 2>&1; then
    printf '%s.zst\n' "$base_path"
  else
    printf '%s.gz\n' "$base_path"
  fi
}

snapshot_archive_list() {
  local archive_path="$1"

  if [[ "$archive_path" == *.zst ]]; then
    zstd -dc "$archive_path" | tar -tf -
  else
    gzip -dc "$archive_path" | tar -tf -
  fi
}

extract_snapshot_file() {
  local archive_path="$1"
  local rel="$2"

  if [[ "$archive_path" == *.zst ]]; then
    zstd -dc "$archive_path" | tar -xOf - "$rel"
  else
    gzip -dc "$archive_path" | tar -xOf - "$rel"
  fi
}

assert_archive_contains() {
  local archive_path="$1"
  local rel="$2"

  if ! snapshot_archive_list "$archive_path" | grep -Fx "$rel" >/dev/null; then
    printf 'Expected archive %s to contain: %s\n' "$archive_path" "$rel" >&2
    exit 1
  fi
}

assert_archive_not_contains() {
  local archive_path="$1"
  local rel="$2"

  if snapshot_archive_list "$archive_path" | grep -Fx "$rel" >/dev/null; then
    printf 'Did not expect archive %s to contain: %s\n' "$archive_path" "$rel" >&2
    exit 1
  fi
}

assert_archive_file_equals() {
  local archive_path="$1"
  local rel="$2"
  local expected_path="$3"
  local extracted_path

  extracted_path="$(mktemp /tmp/codex-snapshot-file.XXXXXX)"
  extract_snapshot_file "$archive_path" "$rel" > "$extracted_path"
  if ! cmp -s "$expected_path" "$extracted_path"; then
    printf 'Expected archive file %s in %s to match %s\n' "$rel" "$archive_path" "$expected_path" >&2
    rm -f "$extracted_path"
    exit 1
  fi

  rm -f "$extracted_path"
}

test_install_generates_portable_launchd_plist() {
  local tmp_home fake_bin log_path plist_path

  tmp_home="$(new_home)"
  fake_bin="$tmp_home/fake-bin"
  log_path="$tmp_home/launchd.log"
  plist_path="$tmp_home/Library/LaunchAgents/io.github.example.codex.snapshot.plist"
  : > "$log_path"
  setup_fake_launchd_tools "$fake_bin" "$log_path"

  HOME="$tmp_home" \
    PATH="$fake_bin:$PATH" \
    CODEX_SNAPSHOT_LABEL="io.github.example.codex.snapshot" \
    CODEX_SNAPSHOT_DIR="$tmp_home/Custom Snapshots" \
    CODEX_SNAPSHOT_STAGING_DIR="$tmp_home/Custom Staging" \
    CODEX_SNAPSHOT_PUBLISH_RENAME_ATTEMPTS=3 \
    CODEX_SNAPSHOT_PUBLISH_RENAME_DELAY_SECONDS=0 \
    CODEX_SNAPSHOT_LEGACY_LABELS="com.example.codex.old-backup com.example.codex.old-snapshot" \
    ONEDRIVE_ROOT="$tmp_home/OneDrive Custom" \
    bash "$REPO_ROOT/scripts/codex_backup_install.sh" > "$tmp_home/install.out"

  assert_file_exists "$plist_path"
  assert_contains "$plist_path" "<key>Label</key><string>io.github.example.codex.snapshot</string>"
  assert_contains "$plist_path" "<string>$REPO_ROOT/scripts/codex_snapshot_daily.sh</string>"
  assert_contains "$plist_path" "<key>EnvironmentVariables</key>"
  assert_contains "$plist_path" "<key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>"
  assert_contains "$plist_path" "<key>CODEX_SNAPSHOT_DIR</key><string>$tmp_home/Custom Snapshots</string>"
  assert_contains "$plist_path" "<key>CODEX_SNAPSHOT_STAGING_DIR</key><string>$tmp_home/Custom Staging</string>"
  assert_contains "$plist_path" "<key>CODEX_SNAPSHOT_PUBLISH_RENAME_ATTEMPTS</key><string>3</string>"
  assert_contains "$plist_path" "<key>CODEX_SNAPSHOT_PUBLISH_RENAME_DELAY_SECONDS</key><string>0</string>"
  assert_contains "$plist_path" "<key>ONEDRIVE_ROOT</key><string>$tmp_home/OneDrive Custom</string>"
  assert_contains "$plist_path" "<key>StandardOutPath</key><string>$tmp_home/Library/Logs/codex_snapshot_daily.out</string>"
  assert_not_contains "$plist_path" "com.example.codex.old"
  assert_contains "$log_path" "launchctl bootout gui/"
  assert_contains "$log_path" "com.example.codex.old-backup"
  assert_contains "$log_path" "com.example.codex.old-snapshot"
  assert_contains "$log_path" "launchctl bootstrap gui/"
  assert_contains "$log_path" "$plist_path"

  cleanup_home "$tmp_home"
}

test_snapshot_skips_missing_source() {
  local tmp_home log_path

  tmp_home="$(new_home)"
  log_path="$tmp_home/Library/Logs/codex_snapshot_daily.log"

  HOME="$tmp_home" bash "$SNAPSHOT_SCRIPT"

  assert_file_exists "$log_path"
  assert_contains "$log_path" "Source rollout directories missing, snapshotting existing mirror"
  assert_contains "$log_path" "No mirrored rollout files found, skipping snapshot."
  cleanup_home "$tmp_home"
}

test_snapshot_updates_mirror_and_archive_from_complete_lines() {
  local tmp_home src_file mirror_file expected_file archive_path

  tmp_home="$(new_home)"
  src_file="$tmp_home/.codex/sessions/day/rollout-partial.jsonl"
  mirror_file="$tmp_home/.dotfiles/codex-backup/mirror/sessions/day/rollout-partial.jsonl"
  expected_file="$tmp_home/expected-rollout-partial.jsonl"
  mkdir -p "$(dirname "$src_file")"

  printf '{"step":1}\n{"step":2' > "$src_file"
  printf '{"step":1}\n' > "$expected_file"

  HOME="$tmp_home" bash "$SNAPSHOT_SCRIPT"

  archive_path="$(snapshot_archive_path "$tmp_home")"
  assert_file_exists "$mirror_file"
  assert_file_exists "$archive_path"
  cmp -s "$expected_file" "$mirror_file"
  assert_archive_contains "$archive_path" "sessions/day/rollout-partial.jsonl"
  assert_archive_file_equals "$archive_path" "sessions/day/rollout-partial.jsonl" "$expected_file"
  assert_not_exists "$tmp_home/OneDrive/Backup/dotfiles/codex/sessions/day/rollout-partial.jsonl"

  printf '}\n' >> "$src_file"
  printf '{"step":1}\n{"step":2}\n' > "$expected_file"
  HOME="$tmp_home" bash "$SNAPSHOT_SCRIPT"

  cmp -s "$expected_file" "$mirror_file"
  assert_archive_file_equals "$archive_path" "sessions/day/rollout-partial.jsonl" "$expected_file"

  cleanup_home "$tmp_home"
}

test_snapshot_keeps_existing_complete_mirror_when_source_has_no_complete_lines() {
  local tmp_home src_file mirror_file expected_file log_path archive_path

  tmp_home="$(new_home)"
  src_file="$tmp_home/.codex/sessions/day/rollout-no-complete-lines.jsonl"
  mirror_file="$tmp_home/.dotfiles/codex-backup/mirror/sessions/day/rollout-no-complete-lines.jsonl"
  expected_file="$tmp_home/expected-no-complete-lines.jsonl"
  log_path="$tmp_home/Library/Logs/codex_snapshot_daily.log"
  mkdir -p "$(dirname "$src_file")"

  printf '{"step":1}\n' > "$expected_file"
  printf '{"step":1}\n' > "$src_file"
  HOME="$tmp_home" bash "$SNAPSHOT_SCRIPT"

  printf '{"step":2' > "$src_file"
  : > "$log_path"
  HOME="$tmp_home" bash "$SNAPSHOT_SCRIPT"

  archive_path="$(snapshot_archive_path "$tmp_home")"
  assert_not_contains "$log_path" "Mirror update sessions/day/rollout-no-complete-lines.jsonl (11 -> 0)"
  assert_file_exists "$mirror_file"
  cmp -s "$expected_file" "$mirror_file"
  assert_archive_file_equals "$archive_path" "sessions/day/rollout-no-complete-lines.jsonl" "$expected_file"

  cleanup_home "$tmp_home"
}

test_snapshot_waits_for_onedrive_readiness_before_unpin() {
  local tmp_home src_file archive_path log_path cli_path state_dir cloud_root

  tmp_home="$(new_home)"
  src_file="$tmp_home/.codex/sessions/day/rollout-2.jsonl"
  archive_path="$(resolved_snapshot_archive_path "$tmp_home")"
  log_path="$tmp_home/Library/Logs/codex_snapshot_daily.log"
  cli_path="$tmp_home/fake-bin/OneDrive"
  state_dir="$tmp_home/fake-onedrive-state"
  cloud_root="$tmp_home/Library/CloudStorage/OneDrive"

  mkdir -p "$(dirname "$src_file")" "$cloud_root"
  ln -s "$cloud_root" "$tmp_home/OneDrive"
  setup_fake_onedrive_cli "$cli_path"
  printf '{"step":1}\n' > "$src_file"

  HOME="$tmp_home" \
  ONEDRIVE_CLI_PATH="$cli_path" \
  ONEDRIVE_UNPIN_READY_ATTEMPTS=3 \
  ONEDRIVE_UNPIN_READY_DELAY_SECONDS=0 \
  TMP_FAKE_ONEDRIVE_STATE="$state_dir" \
  bash "$SNAPSHOT_SCRIPT"

  assert_file_exists "$archive_path"
  assert_contains "$log_path" "OneDrive /getpin not ready for $archive_path (attempt 1/3)"
  assert_contains "$state_dir/cli.log" "/unpin $archive_path"
  assert_not_contains "$log_path" "Skipping OneDrive /unpin: snapshot root is not backed by a real OneDrive CloudStorage symlink or the vendor CLI is unavailable."

  cleanup_home "$tmp_home"
}

test_snapshot_relocates_archived_rollout_and_prunes_duplicate_mirror() {
  local tmp_home live_src archived_src live_mirror archived_mirror log_path archive_path

  tmp_home="$(new_home)"
  live_src="$tmp_home/.codex/sessions/day/rollout-relocate.jsonl"
  archived_src="$tmp_home/.codex/archived_sessions/rollout-relocate.jsonl"
  live_mirror="$tmp_home/.dotfiles/codex-backup/mirror/sessions/day/rollout-relocate.jsonl"
  archived_mirror="$tmp_home/.dotfiles/codex-backup/mirror/archived_sessions/rollout-relocate.jsonl"
  log_path="$tmp_home/Library/Logs/codex_snapshot_daily.log"

  mkdir -p "$(dirname "$live_src")" "$(dirname "$archived_src")"
  printf '{"step":1}\n' > "$live_src"

  HOME="$tmp_home" bash "$SNAPSHOT_SCRIPT"

  assert_file_exists "$live_mirror"

  mv "$live_src" "$archived_src"
  printf '{"step":2}\n' >> "$archived_src"
  mkdir -p "$(dirname "$archived_mirror")"
  cp -p "$live_mirror" "$archived_mirror"
  : > "$log_path"

  HOME="$tmp_home" bash "$SNAPSHOT_SCRIPT"

  archive_path="$(snapshot_archive_path "$tmp_home")"
  assert_contains "$log_path" "Mirror update archived_sessions/rollout-relocate.jsonl"
  assert_contains "$log_path" "Mirror prune stale duplicate sessions/day/rollout-relocate.jsonl -> archived_sessions/rollout-relocate.jsonl"
  assert_not_exists "$live_mirror"
  assert_file_exists "$archived_mirror"
  cmp -s "$archived_src" "$archived_mirror"
  assert_archive_contains "$archive_path" "archived_sessions/rollout-relocate.jsonl"
  assert_archive_not_contains "$archive_path" "sessions/day/rollout-relocate.jsonl"
  assert_archive_file_equals "$archive_path" "archived_sessions/rollout-relocate.jsonl" "$archived_src"

  cleanup_home "$tmp_home"
}

test_snapshot_skips_disappeared_source_during_mirror_sync() {
  local tmp_home disappearing_src disappearing_mirror stable_src log_path hook_path hook_state archive_path

  tmp_home="$(new_home)"
  disappearing_src="$tmp_home/.codex/sessions/day/rollout-disappear.jsonl"
  disappearing_mirror="$tmp_home/.dotfiles/codex-backup/mirror/sessions/day/rollout-disappear.jsonl"
  stable_src="$tmp_home/.codex/sessions/day/rollout-stable.jsonl"
  log_path="$tmp_home/Library/Logs/codex_snapshot_daily.log"
  hook_path="$tmp_home/fake-bin/disappear-hook"
  hook_state="$tmp_home/hook-state"
  mkdir -p "$(dirname "$disappearing_src")"
  printf '{"step":"gone"}\n' > "$disappearing_src"
  printf '{"step":"stay"}\n' > "$stable_src"
  HOME="$tmp_home" bash "$SNAPSHOT_SCRIPT"

  assert_file_exists "$disappearing_mirror"
  : > "$log_path"
  setup_disappear_hook "$hook_path"

  HOME="$tmp_home" \
  CODEX_ROLLOUT_MIRROR_TEST_HOOK="$hook_path" \
  TMP_MIRROR_HOOK_STATE="$hook_state" \
  bash "$SNAPSHOT_SCRIPT"

  archive_path="$(snapshot_archive_path "$tmp_home")"
  assert_contains "$log_path" "Source rollout disappeared during mirror sync, skipping: sessions/day/rollout-disappear.jsonl"
  assert_contains "$log_path" "Suppressing snapshot entry for disappeared rollout this pass: sessions/day/rollout-disappear.jsonl"
  assert_archive_contains "$archive_path" "sessions/day/rollout-stable.jsonl"
  assert_archive_not_contains "$archive_path" "sessions/day/rollout-disappear.jsonl"
  assert_file_exists "$disappearing_mirror"

  cleanup_home "$tmp_home"
}

test_snapshot_skips_rolled_back_relocation_after_disappear() {
  local tmp_home live_src archived_src stable_src log_path hook_path hook_state archive_path

  tmp_home="$(new_home)"
  live_src="$tmp_home/.codex/sessions/day/rollout-relocate-disappear-snapshot.jsonl"
  archived_src="$tmp_home/.codex/archived_sessions/rollout-relocate-disappear-snapshot.jsonl"
  stable_src="$tmp_home/.codex/sessions/day/rollout-stable-relocate-snapshot.jsonl"
  log_path="$tmp_home/Library/Logs/codex_snapshot_daily.log"
  hook_path="$tmp_home/fake-bin/disappear-hook-relocate-snapshot"
  hook_state="$tmp_home/hook-state-relocate-snapshot"
  mkdir -p "$(dirname "$live_src")" "$(dirname "$archived_src")"
  printf '{"step":1}\n' > "$live_src"
  printf '{"stable":1}\n' > "$stable_src"
  HOME="$tmp_home" bash "$SNAPSHOT_SCRIPT"

  mv "$live_src" "$archived_src"
  printf '{"step":2}\n' >> "$archived_src"
  : > "$log_path"
  setup_disappear_hook "$hook_path"

  HOME="$tmp_home" \
  CODEX_ROLLOUT_MIRROR_TEST_HOOK="$hook_path" \
  TMP_MIRROR_HOOK_STATE="$hook_state" \
  TMP_MIRROR_HOOK_STAGE="before_copy" \
  TMP_MIRROR_HOOK_TARGET_REL="archived_sessions/rollout-relocate-disappear-snapshot.jsonl" \
  bash "$SNAPSHOT_SCRIPT"

  archive_path="$(snapshot_archive_path "$tmp_home")"
  assert_file_exists "$archive_path"
  assert_contains "$log_path" "Mirror relocate rollback archived_sessions/rollout-relocate-disappear-snapshot.jsonl -> sessions/day/rollout-relocate-disappear-snapshot.jsonl"
  assert_contains "$log_path" "Suppressing snapshot entry for disappeared rollout this pass: sessions/day/rollout-relocate-disappear-snapshot.jsonl"
  assert_archive_contains "$archive_path" "sessions/day/rollout-stable-relocate-snapshot.jsonl"
  assert_archive_not_contains "$archive_path" "sessions/day/rollout-relocate-disappear-snapshot.jsonl"
  assert_archive_not_contains "$archive_path" "archived_sessions/rollout-relocate-disappear-snapshot.jsonl"

  cleanup_home "$tmp_home"
}

test_snapshot_skips_rolled_back_relocation_after_disappear_during_mirror_sync() {
  local tmp_home live_src archived_src stable_src log_path hook_path hook_state archive_path

  tmp_home="$(new_home)"
  live_src="$tmp_home/.codex/sessions/day/rollout-relocate-disappear-snapshot-sync.jsonl"
  archived_src="$tmp_home/.codex/archived_sessions/rollout-relocate-disappear-snapshot-sync.jsonl"
  stable_src="$tmp_home/.codex/sessions/day/rollout-stable-relocate-snapshot-sync.jsonl"
  log_path="$tmp_home/Library/Logs/codex_snapshot_daily.log"
  hook_path="$tmp_home/fake-bin/disappear-hook-relocate-snapshot-sync"
  hook_state="$tmp_home/hook-state-relocate-snapshot-sync"
  mkdir -p "$(dirname "$live_src")" "$(dirname "$archived_src")"
  printf '{"step":1}\n' > "$live_src"
  printf '{"stable":1}\n' > "$stable_src"
  HOME="$tmp_home" bash "$SNAPSHOT_SCRIPT"

  mv "$live_src" "$archived_src"
  : > "$log_path"
  setup_disappear_hook "$hook_path"

  HOME="$tmp_home" \
  CODEX_ROLLOUT_MIRROR_TEST_HOOK="$hook_path" \
  TMP_MIRROR_HOOK_STATE="$hook_state" \
  TMP_MIRROR_HOOK_STAGE="before_stat" \
  TMP_MIRROR_HOOK_TARGET_REL="archived_sessions/rollout-relocate-disappear-snapshot-sync.jsonl" \
  bash "$SNAPSHOT_SCRIPT"

  archive_path="$(snapshot_archive_path "$tmp_home")"
  assert_file_exists "$archive_path"
  assert_contains "$log_path" "Mirror relocate sessions/day/rollout-relocate-disappear-snapshot-sync.jsonl -> archived_sessions/rollout-relocate-disappear-snapshot-sync.jsonl"
  assert_contains "$log_path" "Mirror relocate rollback archived_sessions/rollout-relocate-disappear-snapshot-sync.jsonl -> sessions/day/rollout-relocate-disappear-snapshot-sync.jsonl"
  assert_contains "$log_path" "Source rollout disappeared during mirror sync, skipping: archived_sessions/rollout-relocate-disappear-snapshot-sync.jsonl"
  assert_contains "$log_path" "Suppressing snapshot entry for disappeared rollout this pass: sessions/day/rollout-relocate-disappear-snapshot-sync.jsonl"
  assert_archive_contains "$archive_path" "sessions/day/rollout-stable-relocate-snapshot-sync.jsonl"
  assert_archive_not_contains "$archive_path" "sessions/day/rollout-relocate-disappear-snapshot-sync.jsonl"
  assert_archive_not_contains "$archive_path" "archived_sessions/rollout-relocate-disappear-snapshot-sync.jsonl"

  cleanup_home "$tmp_home"
}

test_snapshot_skips_empty_source() {
  local tmp_home log_path

  tmp_home="$(new_home)"
  mkdir -p "$tmp_home/.codex/sessions"
  log_path="$tmp_home/Library/Logs/codex_snapshot_daily.log"

  HOME="$tmp_home" bash "$SNAPSHOT_SCRIPT"

  assert_file_exists "$log_path"
  assert_contains "$log_path" "No mirrored rollout files found, skipping snapshot."
  cleanup_home "$tmp_home"
}

test_snapshot_can_rerun_same_day() {
  local tmp_home archive_path log_path

  tmp_home="$(new_home)"
  log_path="$tmp_home/Library/Logs/codex_snapshot_daily.log"
  mkdir -p "$tmp_home/.codex/sessions/day"
  mkdir -p "$tmp_home/.codex/archived_sessions"
  printf '{"step":1}\n' > "$tmp_home/.codex/sessions/day/rollout-1.jsonl"
  printf '{"archived":1}\n' > "$tmp_home/.codex/archived_sessions/rollout-archived.jsonl"

  HOME="$tmp_home" bash "$SNAPSHOT_SCRIPT"
  printf '{"step":1}\n{"step":2}\n' > "$tmp_home/.codex/sessions/day/rollout-1.jsonl"
  HOME="$tmp_home" bash "$SNAPSHOT_SCRIPT"

  archive_path="$(snapshot_archive_path "$tmp_home")"
  assert_file_exists "$archive_path"
  assert_archive_contains "$archive_path" "sessions/day/rollout-1.jsonl"
  assert_archive_contains "$archive_path" "archived_sessions/rollout-archived.jsonl"
  assert_contains "$log_path" "Skipping OneDrive /unpin: snapshot root is not backed by a real OneDrive CloudStorage symlink or the vendor CLI is unavailable."

  cleanup_home "$tmp_home"
}

test_snapshot_retries_transient_publish_rename_failure() {
  local tmp_home archive_path log_path fake_bin state_dir src_file staging_dir

  tmp_home="$(new_home)"
  archive_path="$(snapshot_archive_path "$tmp_home")"
  log_path="$tmp_home/Library/Logs/codex_snapshot_daily.log"
  fake_bin="$tmp_home/fake-bin"
  state_dir="$tmp_home/fake-mv-state"
  src_file="$tmp_home/.codex/sessions/day/rollout-publish-retry.jsonl"
  staging_dir="$tmp_home/snapshot-staging"

  mkdir -p "$(dirname "$src_file")"
  setup_fake_mv_once "$fake_bin/mv"
  printf '{"step":1}\n' > "$src_file"

  HOME="$tmp_home" \
  PATH="$fake_bin:$PATH" \
  CODEX_SNAPSHOT_STAGING_DIR="$staging_dir" \
  CODEX_SNAPSHOT_PUBLISH_RENAME_ATTEMPTS=2 \
  CODEX_SNAPSHOT_PUBLISH_RENAME_DELAY_SECONDS=0 \
  TMP_FAKE_MV_STATE="$state_dir" \
  TMP_FAKE_MV_FAIL_DST="$archive_path" \
  bash "$SNAPSHOT_SCRIPT"

  assert_file_exists "$archive_path"
  assert_archive_contains "$archive_path" "sessions/day/rollout-publish-retry.jsonl"
  assert_contains "$log_path" "Snapshot publish rename failed (attempt 1/2)"
  assert_contains "$state_dir/mv.log" "$archive_path"
  if find "$tmp_home/OneDrive/Backup/dotfiles/codex/snapshots" -name '*.tmp.*' -print | grep -q .; then
    printf 'Did not expect snapshot tmp files under OneDrive snapshots\n' >&2
    exit 1
  fi
  if find "$staging_dir" -name '*.tmp.*' -print | grep -q .; then
    printf 'Did not expect snapshot tmp files left in staging\n' >&2
    exit 1
  fi

  cleanup_home "$tmp_home"
}

test_snapshot_preserves_staging_file_when_publish_retries_are_exhausted() {
  local tmp_home archive_path log_path fake_bin state_dir src_file staging_dir

  tmp_home="$(new_home)"
  archive_path="$(snapshot_archive_path "$tmp_home")"
  log_path="$tmp_home/Library/Logs/codex_snapshot_daily.log"
  fake_bin="$tmp_home/fake-bin"
  state_dir="$tmp_home/fake-mv-state"
  src_file="$tmp_home/.codex/sessions/day/rollout-publish-fails.jsonl"
  staging_dir="$tmp_home/snapshot-staging"

  mkdir -p "$(dirname "$src_file")"
  setup_fake_mv_once "$fake_bin/mv"
  printf '{"step":1}\n' > "$src_file"

  if HOME="$tmp_home" \
    PATH="$fake_bin:$PATH" \
    CODEX_SNAPSHOT_STAGING_DIR="$staging_dir" \
    CODEX_SNAPSHOT_PUBLISH_RENAME_ATTEMPTS=1 \
    CODEX_SNAPSHOT_PUBLISH_RENAME_DELAY_SECONDS=0 \
    TMP_FAKE_MV_STATE="$state_dir" \
    TMP_FAKE_MV_FAIL_DST="$archive_path" \
    bash "$SNAPSHOT_SCRIPT"; then
    printf 'Expected snapshot script to fail after exhausted publish retries\n' >&2
    exit 1
  fi

  assert_not_exists "$archive_path"
  assert_contains "$log_path" "Snapshot publish rename failed after 1 attempts"
  if ! find "$staging_dir" -name '*.tmp.*' -print | grep -q .; then
    printf 'Expected snapshot tmp file to remain in staging after publish failure\n' >&2
    exit 1
  fi

  cleanup_home "$tmp_home"
}

test_snapshot_uses_existing_mirror_when_source_missing() {
  local tmp_home archive_path mirror_file

  tmp_home="$(new_home)"
  mirror_file="$tmp_home/.dotfiles/codex-backup/mirror/sessions/day/rollout-from-mirror.jsonl"
  mkdir -p "$(dirname "$mirror_file")"
  printf '{"step":1}\n' > "$mirror_file"

  HOME="$tmp_home" bash "$SNAPSHOT_SCRIPT"

  archive_path="$(snapshot_archive_path "$tmp_home")"
  assert_file_exists "$archive_path"
  assert_archive_contains "$archive_path" "sessions/day/rollout-from-mirror.jsonl"
  assert_not_exists "$tmp_home/OneDrive/Backup/dotfiles/codex/sessions/day/rollout-from-mirror.jsonl"

  cleanup_home "$tmp_home"
}

test_sync_tolerates_missing_current_mirror_during_stat() {
  local tmp_home source_path mirror_path log_path

  tmp_home="$(new_home)"
  source_path="$tmp_home/.codex/sessions/day/rollout-dst-stat-race.jsonl"
  mirror_path="$tmp_home/.dotfiles/codex-backup/mirror/sessions/day/rollout-dst-stat-race.jsonl"
  log_path="$tmp_home/sync-dst-stat-race.log"
  mkdir -p "$(dirname "$source_path")" "$(dirname "$mirror_path")"
  : > "$log_path"
  printf '{"step":2}\n' > "$source_path"
  printf '{"step":1}\n' > "$mirror_path"

  (
    set -euo pipefail
    HOME="$tmp_home"
    LOG="$log_path"
    RACE_MARKER="$tmp_home/dst-stat-race.marker"
    RACE_PATH="$mirror_path"

    # shellcheck source=scripts/codex_rollout_mirror_common.sh
    . "$REPO_ROOT/scripts/codex_rollout_mirror_common.sh"

    file_size_if_present() {
      local path="$1"
      local size

      if [ "$path" = "$RACE_PATH" ] && [ ! -e "$RACE_MARKER" ]; then
        rm -f "$path"
        : > "$RACE_MARKER"
      fi

      if size=$(file_size "$path" 2>/dev/null); then
        printf '%s\n' "$size"
        return 0
      fi

      [ -e "$path" ] || return 1
      return 2
    }

    sync_codex_rollout_mirror
  )

  assert_file_exists "$mirror_path"
  assert_contains "$mirror_path" '{"step":2}'
  assert_not_contains "$log_path" "Failed to stat mirror rollout during sync: sessions/day/rollout-dst-stat-race.jsonl"

  cleanup_home "$tmp_home"
}

test_sync_tolerates_missing_duplicate_mirror_during_stat() {
  local tmp_home source_path mirror_path stale_duplicate_path log_path

  tmp_home="$(new_home)"
  source_path="$tmp_home/.codex/archived_sessions/rollout-duplicate-stat-race.jsonl"
  mirror_path="$tmp_home/.dotfiles/codex-backup/mirror/archived_sessions/rollout-duplicate-stat-race.jsonl"
  stale_duplicate_path="$tmp_home/.dotfiles/codex-backup/mirror/sessions/day/rollout-duplicate-stat-race.jsonl"
  log_path="$tmp_home/sync-duplicate-stat-race.log"
  mkdir -p "$(dirname "$source_path")" "$(dirname "$mirror_path")" "$(dirname "$stale_duplicate_path")"
  : > "$log_path"
  printf '{"step":2}' > "$source_path"
  printf '{"step":1}\n' > "$mirror_path"
  cp "$mirror_path" "$stale_duplicate_path"

  (
    set -euo pipefail
    HOME="$tmp_home"
    LOG="$log_path"
    RACE_MARKER="$tmp_home/duplicate-stat-race.marker"
    RACE_PATH="$stale_duplicate_path"

    # shellcheck source=scripts/codex_rollout_mirror_common.sh
    . "$REPO_ROOT/scripts/codex_rollout_mirror_common.sh"

    file_size_if_present() {
      local path="$1"
      local size

      if [ "$path" = "$RACE_PATH" ] && [ ! -e "$RACE_MARKER" ]; then
        rm -f "$path"
        : > "$RACE_MARKER"
      fi

      if size=$(file_size "$path" 2>/dev/null); then
        printf '%s\n' "$size"
        return 0
      fi

      [ -e "$path" ] || return 1
      return 2
    }

    sync_codex_rollout_mirror
  )

  assert_file_exists "$mirror_path"
  assert_not_exists "$stale_duplicate_path"
  assert_contains "$mirror_path" '{"step":1}'
  assert_not_contains "$log_path" "Failed to stat duplicate mirror rollout during sync: sessions/day/rollout-duplicate-stat-race.jsonl"

  cleanup_home "$tmp_home"
}

test_snapshot_handles_many_relocations_under_low_maxfiles() {
  local tmp_home archive_path err_path sample_live sample_archived sample_id i

  tmp_home="$(new_home)"
  err_path="$tmp_home/low-maxfiles.err"
  mkdir -p "$tmp_home/.codex/sessions/day" "$tmp_home/.codex/archived_sessions/batch"

  for i in $(seq 1 160); do
    printf -v sample_id '%03d' "$i"
    printf '{"step":%s}\n' "$i" > "$tmp_home/.codex/sessions/day/rollout-fd-$sample_id.jsonl"
  done

  HOME="$tmp_home" bash "$SNAPSHOT_SCRIPT"

  for i in $(seq 1 160); do
    printf -v sample_id '%03d' "$i"
    mv \
      "$tmp_home/.codex/sessions/day/rollout-fd-$sample_id.jsonl" \
      "$tmp_home/.codex/archived_sessions/batch/rollout-fd-$sample_id.jsonl"
    printf '{"archived":%s}\n' "$i" >> "$tmp_home/.codex/archived_sessions/batch/rollout-fd-$sample_id.jsonl"
  done

  (
    ulimit -n 64
    HOME="$tmp_home" bash "$SNAPSHOT_SCRIPT"
  ) 2>"$err_path"

  archive_path="$(snapshot_archive_path "$tmp_home")"
  sample_live="sessions/day/rollout-fd-001.jsonl"
  sample_archived="archived_sessions/batch/rollout-fd-001.jsonl"

  assert_file_exists "$archive_path"
  assert_file_exists "$tmp_home/.dotfiles/codex-backup/mirror/$sample_archived"
  assert_not_exists "$tmp_home/.dotfiles/codex-backup/mirror/$sample_live"
  assert_archive_contains "$archive_path" "$sample_archived"
  assert_archive_not_contains "$archive_path" "$sample_live"
  assert_not_contains "$err_path" "Too many open files"
  assert_not_contains "$err_path" "Bad file descriptor"

  cleanup_home "$tmp_home"
}

test_snapshot_skips_missing_source
test_snapshot_updates_mirror_and_archive_from_complete_lines
test_snapshot_keeps_existing_complete_mirror_when_source_has_no_complete_lines
test_snapshot_waits_for_onedrive_readiness_before_unpin
test_snapshot_relocates_archived_rollout_and_prunes_duplicate_mirror
test_snapshot_skips_disappeared_source_during_mirror_sync
test_snapshot_skips_rolled_back_relocation_after_disappear
test_snapshot_skips_rolled_back_relocation_after_disappear_during_mirror_sync
test_snapshot_skips_empty_source
test_snapshot_can_rerun_same_day
test_snapshot_retries_transient_publish_rename_failure
test_snapshot_preserves_staging_file_when_publish_retries_are_exhausted
test_snapshot_uses_existing_mirror_when_source_missing
test_sync_tolerates_missing_current_mirror_during_stat
test_sync_tolerates_missing_duplicate_mirror_during_stat
test_snapshot_handles_many_relocations_under_low_maxfiles
test_install_generates_portable_launchd_plist
