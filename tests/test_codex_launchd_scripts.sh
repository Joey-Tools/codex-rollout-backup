#!/bin/bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SNAPSHOT_SCRIPT="$REPO_ROOT/scripts/codex_snapshot_daily.sh"
SNAPSHOT_LOCK_HELPER="$REPO_ROOT/scripts/codex_snapshot_lock.py"
TEST_DEFAULT_COMPUTER_NAME="${CODEX_BACKUP_COMPUTER_NAME:-Codex Test Computer}"
export CODEX_BACKUP_COMPUTER_NAME="$TEST_DEFAULT_COMPUTER_NAME"

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

assert_no_extended_acl() {
  local path="$1"
  local listing

  listing="$(LC_ALL=C /bin/ls -lde "$path")"
  case "$listing" in
    *$'\n'*)
      printf 'Did not expect an extended ACL on: %s\n' "$path" >&2
      exit 1
      ;;
  esac
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

wait_for_file() {
  local path="$1"
  local attempts="${2:-200}"
  local attempt=0

  while [ "$attempt" -lt "$attempts" ]; do
    [ -e "$path" ] && return 0
    /bin/sleep 0.05
    attempt=$((attempt + 1))
  done

  return 1
}

run_with_deadline() {
  local status_path="$1"
  local pid
  shift

  (
    set +e
    "$@"
    printf '%s\n' "$?" > "$status_path"
  ) &
  pid=$!
  if ! wait_for_file "$status_path" 200; then
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
    return 124
  fi
  wait "$pid"
  cat "$status_path"
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

setup_fake_cp_publish_failure() {
  local cp_path="$1"

  mkdir -p "$(dirname "$cp_path")"
  cat > "$cp_path" <<'EOF'
#!/bin/bash
set -euo pipefail

state_dir="${TMP_FAKE_CP_STATE:?}"
fail_dir="${TMP_FAKE_CP_FAIL_DIR:?}"
log_file="$state_dir/cp.log"
dst="${@: -1}"

mkdir -p "$state_dir"
printf 'cp %s\n' "$*" >> "$log_file"

case "$dst" in
  "$fail_dir"/codex-rollouts-*.tar.*.tmp.*)
    printf 'partial publish tmp\n' > "$dst"
    printf 'cp: simulated publish copy failure for %s\n' "$dst" >&2
    exit 73
    ;;
esac

exec /bin/cp "$@"
EOF
  chmod +x "$cp_path"
}

setup_fake_validating_sleep() {
  local sleep_path="$1"

  mkdir -p "$(dirname "$sleep_path")"
  cat > "$sleep_path" <<'EOF'
#!/bin/bash
set -euo pipefail

state_dir="${TMP_FAKE_SLEEP_STATE:?}"
log_file="$state_dir/sleep.log"
delay="${1:-}"

mkdir -p "$state_dir"
printf 'sleep %s\n' "$delay" >> "$log_file"

if [[ "$delay" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
  exit 0
fi

printf 'sleep: invalid time interval %s\n' "$delay" >&2
exit 97
EOF
  chmod +x "$sleep_path"
}

setup_fake_reflink_maintenance_python3() {
  local python_path="$1"

  mkdir -p "$(dirname "$python_path")"
  cat > "$python_path" <<'EOF'
#!/bin/bash
set -euo pipefail

state_dir="${TMP_FAKE_REFLINK_RETRY_STATE:?}"
required_path="${TMP_FAKE_REFLINK_RETRY_REQUIRE_PATH:-}"
real_python="${TMP_REAL_PYTHON3:-/usr/bin/python3}"
order_log="${TMP_FAKE_REFLINK_ORDER_LOG:-}"

case "${1:-}" in
  */codex_snapshot_lock.py|*/codex_snapshot_path.py|*/codex_rollout_mirror_copy.py)
    exec "$real_python" "$@"
    ;;
esac

mkdir -p "$state_dir"
printf '%s\n' "$*" >> "$state_dir/python3.log"
command="${2:-}"
case "$command" in
  recover)
    status="${TMP_FAKE_REFLINK_RECOVER_STATUS:-0}"
    ;;
  retry)
    status="${TMP_FAKE_REFLINK_RETRY_STATUS:-0}"
    if [ -n "$required_path" ] && [ ! -f "$required_path" ]; then
      printf 'Expected mirror file before reflink retry: %s\n' "$required_path" >&2
      exit 98
    fi
    ;;
  *)
    exec "$real_python" "$@"
    ;;
esac
if [ -n "$order_log" ]; then
  printf '%s\n' "$command" >> "$order_log"
fi
printf '{"command":"%s","status":%s}\n' "$command" "$status"
exit "$status"
EOF
  chmod +x "$python_path"
}

setup_bytecode_guard_python3() {
  local python_path="$1"

  mkdir -p "$(dirname "$python_path")"
  cat > "$python_path" <<'EOF'
#!/bin/bash
set -euo pipefail

state_dir="${TMP_BYTECODE_GUARD_STATE:?}"
bytecode_setting="${PYTHONDONTWRITEBYTECODE-<unset>}"

mkdir -p "$state_dir"
printf 'PYTHONDONTWRITEBYTECODE=%s %s\n' "$bytecode_setting" "$*" >> "$state_dir/python3.log"
if [ "$bytecode_setting" != "1" ]; then
  printf 'Expected PYTHONDONTWRITEBYTECODE=1, got: %s\n' "$bytecode_setting" >&2
  exit 97
fi

case "${1:-}" in
  */codex_snapshot_lock.py)
    exit 0
    ;;
  */codex_snapshot_path.py|*/codex_snapshot_validate_trees.py)
    exec "${TMP_REAL_PYTHON3:-/usr/bin/python3}" "$@"
    ;;
  */codex_repair_rollout_reflinks.py)
    printf '{"command":"%s","status":0}\n' "${2:-}"
    exit 0
    ;;
  *)
    printf 'Unexpected Python child: %s\n' "$*" >&2
    exit 98
    ;;
esac
EOF
  chmod +x "$python_path"
}

setup_fake_mirror_copy_helper() {
  local helper_path="$1"

  mkdir -p "$(dirname "$helper_path")"
  cat > "$helper_path" <<'EOF'
#!/usr/bin/env python3
import json
import os
import sys


def option(name: str) -> str:
    try:
        return sys.argv[sys.argv.index(name) + 1]
    except (ValueError, IndexError):
        return ""


command = sys.argv[1] if len(sys.argv) > 1 else ""
source = option("--source")
target_source = os.environ.get("TMP_FAKE_MIRROR_COPY_TARGET_SOURCE", "")
status = int(os.environ.get("TMP_FAKE_MIRROR_COPY_STATUS", "0"))
log_path = os.environ["TMP_FAKE_MIRROR_COPY_LOG"]

with open(log_path, "a", encoding="utf-8") as log_file:
    log_file.write(f"{command} {source} status={status}\n")

if source == target_source and command == "sync-one":
    print(json.dumps({"fake": True, "source": source}, sort_keys=True))
    raise SystemExit(status)

if source == target_source and command == "validate-receipt":
    sys.stdin.buffer.read()
    if status == 75:
        print("deferred\tnull\tnull\tnull")
    elif status == 2:
        print("fatal\tnull\tnull\tnull")
    else:
        raise SystemExit(65)
    raise SystemExit(0)

real_helper = os.environ["TMP_REAL_MIRROR_COPY_HELPER"]
os.execv(sys.executable, [sys.executable, real_helper, *sys.argv[1:]])
EOF
  chmod +x "$helper_path"
}

setup_order_recording_tar() {
  local tar_path="$1"

  mkdir -p "$(dirname "$tar_path")"
  cat > "$tar_path" <<'EOF'
#!/bin/bash
set -euo pipefail

printf 'archive\n' >> "${TMP_FAKE_REFLINK_ORDER_LOG:?}"
exec "${TMP_REAL_TAR:-/usr/bin/tar}" "$@"
EOF
  chmod +x "$tar_path"
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

setup_forbidden_mirror_hook() {
  local hook_path="$1"

  mkdir -p "$(dirname "$hook_path")"
  cat > "$hook_path" <<'EOF'
#!/bin/bash
set -euo pipefail

: > "${TMP_FORBIDDEN_MIRROR_HOOK_MARKER:?}"
exit "${TMP_FORBIDDEN_MIRROR_HOOK_STATUS:-99}"
EOF
  chmod +x "$hook_path"
}

setup_partial_find_failure() {
  local find_path="$1"

  mkdir -p "$(dirname "$find_path")"
  cat > "$find_path" <<'EOF'
#!/bin/bash
set -euo pipefail

target_root="${TMP_PARTIAL_FIND_FAIL_ROOT:?}"
partial_path="${TMP_PARTIAL_FIND_OUTPUT_PATH:?}"
status="${TMP_PARTIAL_FIND_STATUS:?}"

if [ "${1:-}" = "$target_root" ]; then
  printf '%s\0' "$partial_path"
  exit "$status"
fi
exec /usr/bin/find "$@"
EOF
  chmod +x "$find_path"
}

setup_forbidden_tar() {
  local tar_path="$1"

  mkdir -p "$(dirname "$tar_path")"
  cat > "$tar_path" <<'EOF'
#!/bin/bash
set -euo pipefail

: > "${TMP_FORBIDDEN_TAR_MARKER:?}"
exit 98
EOF
  chmod +x "$tar_path"
}

setup_partial_failure_zstd() {
  local zstd_path="$1"

  mkdir -p "$(dirname "$zstd_path")"
  cat > "$zstd_path" <<'EOF'
#!/bin/bash
set -euo pipefail

output=""
while [ "$#" -gt 0 ]; do
  if [ "$1" = "-o" ]; then
    shift
    output="${1:?}"
  fi
  shift
done
[ -n "$output" ] || exit 96
printf 'partial compressed stream' > "$output"
exit 2
EOF
  chmod +x "$zstd_path"
}

setup_partial_sync_tmp_mktemp() {
  local mktemp_path="$1"

  mkdir -p "$(dirname "$mktemp_path")"
  cat > "$mktemp_path" <<'EOF'
#!/bin/bash
set -euo pipefail

template="${@: -1}"
case "$template" in
  */codex-rollout-sync.XXXXXX)
    state_dir="${TMP_PARTIAL_SYNC_MKTEMP_STATE:?}"
    sync_dir="${TMPDIR:?}/codex-rollout-sync.injected.$$"
    mkdir -p "$state_dir/blocking-directory"
    mkdir -m 700 "$sync_dir"
    : > "$sync_dir/source-paths"
    : > "$sync_dir/sources"
    ln -s "$state_dir/blocking-directory" "$sync_dir/source-rels"
    printf '%s\n' "$sync_dir"
    ;;
  *)
    exec /usr/bin/mktemp "$@"
    ;;
esac
EOF
  chmod +x "$mktemp_path"
}

setup_source_replacement_hook() {
  local hook_path="$1"

  mkdir -p "$(dirname "$hook_path")"
  cat > "$hook_path" <<'EOF'
#!/bin/bash
set -euo pipefail

stage="${1:?}"
path="${2:?}"
detail="${3:?}"
state_dir="${TMP_MIRROR_REPLACEMENT_STATE:?}"
target_stage="${TMP_MIRROR_REPLACEMENT_STAGE:-after_source_open}"
target_path="${TMP_MIRROR_REPLACEMENT_TARGET_PATH:-}"
target_rel="${TMP_MIRROR_REPLACEMENT_TARGET_REL:-}"
mode="${TMP_MIRROR_REPLACEMENT_MODE:?}"

[ "$stage" = "$target_stage" ] || exit 0
if [ -n "$target_path" ]; then
  [ "$path" = "$target_path" ] || exit 0
else
  [ -n "$target_rel" ] || exit 64
  [ "$detail" = "$target_rel" ] || exit 0
fi
mkdir -p "$state_dir"
mkdir "$state_dir/claimed" 2>/dev/null || exit 0

case "$mode" in
  fifo)
    mv "$path" "$path.opened"
    mkfifo "$path"
    ;;
  symlink)
    mv "$path" "$path.opened"
    ln -s "${TMP_MIRROR_REPLACEMENT_OUTSIDE:?}" "$path"
    ;;
  regular)
    mv "$path" "$path.opened"
    printf '{"replacement":true}\n' > "$path"
    ;;
  parent)
    parent="$(dirname "$path")"
    mv "$parent" "$parent.opened"
    mkdir "$parent"
    printf '{"replacement":true}\n' > "$path"
    ;;
  same-inode-rewrite)
    identity_before="$(/usr/bin/stat -f '%d:%i' "$path")"
    size_before="$(/usr/bin/stat -f%z "$path")"
    printf '%s' "${TMP_MIRROR_REPLACEMENT_REWRITE:?}" > "$path"
    identity_after="$(/usr/bin/stat -f '%d:%i' "$path")"
    size_after="$(/usr/bin/stat -f%z "$path")"
    [ "$identity_after" = "$identity_before" ] || exit 65
    [ "$size_after" = "$size_before" ] || exit 66
    printf '%s\n' "$identity_after" > "$state_dir/rewrite.identity"
    ;;
  *)
    printf 'Unsupported source replacement mode: %s\n' "$mode" >&2
    exit 64
    ;;
esac
EOF
  chmod +x "$hook_path"
}

setup_mirror_helper_observer_hook() {
  local hook_path="$1"

  mkdir -p "$(dirname "$hook_path")"
  cat > "$hook_path" <<'EOF'
#!/bin/bash
set -euo pipefail

stage="${1:?}"
source_path="${2:?}"
destination_path="${3:?}"
target_source="${TMP_MIRROR_OBSERVER_SOURCE:?}"
state_dir="${TMP_MIRROR_OBSERVER_STATE:?}"

[ "$source_path" = "$target_source" ] || exit 0
mkdir -p "$state_dir"
printf '%s\n' "$stage" >> "$state_dir/stages.log"

case "$stage" in
  after_stage_create)
    destination_parent="$(dirname "$destination_path")"
    stage_dir="$(find "$destination_parent" -mindepth 1 -maxdepth 1 -type d -print -quit)"
    [ -n "$stage_dir" ] || exit 81
    count="$(find "$destination_parent" -mindepth 1 -maxdepth 1 -type d -print | wc -l | tr -d ' ')"
    [ "$count" -eq 1 ] || exit 82
    [ "$(/usr/bin/stat -f%Lp "$stage_dir")" = "700" ] || exit 83
    acl_listing="$(LC_ALL=C /bin/ls -lde "$stage_dir")"
    case "$acl_listing" in
      *$'\n'*) exit 84 ;;
    esac
    if find "$stage_dir" -mindepth 1 -maxdepth 1 -print -quit | grep -q .; then
      exit 85
    fi
    printf '%s\n' "$stage_dir" > "$state_dir/stage.path"
    ;;
  after_clone | before_publish)
    stage_dir="$(cat "$state_dir/stage.path")"
    [ ! -e "$destination_path" ] && [ ! -L "$destination_path" ] || exit 86
    count="$(find "$stage_dir" -mindepth 1 -maxdepth 1 -type f -print | wc -l | tr -d ' ')"
    [ "$count" -eq 1 ] || exit 87
    ;;
  after_publish)
    [ -f "$destination_path" ] && [ ! -L "$destination_path" ] || exit 88
    ;;
esac
EOF
  chmod +x "$hook_path"
}

setup_lock_replacement_hook() {
  local hook_path="$1"

  mkdir -p "$(dirname "$hook_path")"
  cat > "$hook_path" <<'EOF'
#!/bin/bash
set -euo pipefail

stage="${1:?}"
state_root="${2:?}"
state_dir="${TMP_LOCK_REPLACEMENT_STATE:?}"
target_stage="${TMP_LOCK_REPLACEMENT_STAGE:-before_flock}"

mkdir -p "$state_dir"
[ "$stage" = "$target_stage" ] || exit 0
mkdir "$state_dir/claimed" 2>/dev/null || exit 0
mv "$state_root/snapshot.lock" "$state_root/snapshot.lock.opened"
printf 'replacement\n' > "$state_root/snapshot.lock"
chmod 600 "$state_root/snapshot.lock"
EOF
  chmod +x "$hook_path"
}

setup_state_root_replacement_hook() {
  local hook_path="$1"

  mkdir -p "$(dirname "$hook_path")"
  cat > "$hook_path" <<'EOF'
#!/bin/bash
set -euo pipefail

stage="${1:?}"
state_root="${2:?}"
state_dir="${TMP_STATE_ROOT_REPLACEMENT_STATE:?}"
target_stage="${TMP_STATE_ROOT_REPLACEMENT_STAGE:?}"
ancestor="${TMP_STATE_ROOT_REPLACEMENT_ANCESTOR:?}"

mkdir -p "$state_dir"
[ "$stage" = "$target_stage" ] || exit 0
mkdir "$state_dir/claimed" 2>/dev/null || exit 0
mv "$ancestor" "$ancestor.opened"
mkdir -p "$state_root"
EOF
  chmod +x "$hook_path"
}

setup_blocking_zstd() {
  local zstd_path="$1"

  mkdir -p "$(dirname "$zstd_path")"
  cat > "$zstd_path" <<'EOF'
#!/bin/bash
set -euo pipefail

state_dir="${TMP_BLOCKING_ZSTD_STATE:?}"
real_zstd="${TMP_REAL_ZSTD:?}"
output=""
previous=""

mkdir -p "$state_dir"
for argument in "$@"; do
  if [ "$previous" = "-o" ]; then
    output="$argument"
    break
  fi
  previous="$argument"
done
[ -n "$output" ] || exit 96
: > "$output"
if mkdir "$state_dir/holder" 2>/dev/null; then
  : > "$state_dir/entered"
  attempt=0
  while [ ! -e "$state_dir/release" ] && [ "$attempt" -lt 200 ]; do
    /bin/sleep 0.05
    attempt=$((attempt + 1))
  done
  [ -e "$state_dir/release" ] || exit 99
fi
exec "$real_zstd" "$@"
EOF
  chmod +x "$zstd_path"
}

run_mirror_sync_in_isolated_shell() (
  set -euo pipefail

  # shellcheck source=scripts/codex_rollout_mirror_common.sh
  . "$REPO_ROOT/scripts/codex_rollout_mirror_common.sh"
  sync_codex_rollout_mirror
)

run_mirror_sync_without_log_in_isolated_shell() (
  set -euo pipefail
  unset LOG

  # shellcheck source=scripts/codex_rollout_mirror_common.sh
  . "$REPO_ROOT/scripts/codex_rollout_mirror_common.sh"
  sync_codex_rollout_mirror
)

snapshot_archive_path() {
  local tmp_home="$1"
  local computer_name="${2:-$TEST_DEFAULT_COMPUTER_NAME}"
  local base_path

  base_path="$tmp_home/OneDrive/Backup/dotfiles/codex/$computer_name/snapshots/codex-rollouts-$(date +%Y-%m-%d).tar"
  if command -v zstd >/dev/null 2>&1; then
    printf '%s.zst\n' "$base_path"
  else
    printf '%s.gz\n' "$base_path"
  fi
}

resolved_snapshot_archive_path() {
  local tmp_home="$1"
  local computer_name="${2:-$TEST_DEFAULT_COMPUTER_NAME}"
  local base_path

  base_path="$tmp_home/Library/CloudStorage/OneDrive/Backup/dotfiles/codex/$computer_name/snapshots/codex-rollouts-$(date +%Y-%m-%d).tar"
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

snapshot_archive_verbose_list() {
  local archive_path="$1"

  if [[ "$archive_path" == *.zst ]]; then
    zstd -dc "$archive_path" | tar -tvf -
  else
    gzip -dc "$archive_path" | tar -tvf -
  fi
}

extract_snapshot_file() {
  local archive_path="$1"
  local rel="$2"
  local python_code

  python_code='
import sys
import tarfile

archive = tarfile.open(fileobj=sys.stdin.buffer, mode="r|")
found = False
for member in archive:
    if member.name == sys.argv[1]:
        source = archive.extractfile(member)
        if source is None:
            sys.exit(1)
        while True:
            chunk = source.read(65536)
            if not chunk:
                break
            sys.stdout.buffer.write(chunk)
        found = True
if not found:
    sys.exit(1)
'

  if [[ "$archive_path" == *.zst ]]; then
    zstd -dc "$archive_path" | python3 -c "$python_code" "$rel"
  else
    gzip -dc "$archive_path" | python3 -c "$python_code" "$rel"
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

assert_archive_path_exists() {
  local archive_path="$1"
  local rel="$2"

  if snapshot_archive_list "$archive_path" | grep -Fx -e "$rel" -e "$rel/" >/dev/null; then
    return 0
  fi
  printf 'Expected archive %s to contain path: %s\n' "$archive_path" "$rel" >&2
  exit 1
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
    CODEX_BACKUP_COMPUTER_NAME="Office & Studio" \
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
  assert_contains "$plist_path" "<key>CODEX_BACKUP_COMPUTER_NAME</key><string>Office &amp; Studio</string>"
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
  assert_contains "$log_path" "Source rollout directories missing, skipping snapshot."
  assert_not_exists "$(snapshot_archive_path "$tmp_home")"
  cleanup_home "$tmp_home"
}

test_snapshot_disables_bytecode_writes_for_python_children() {
  local tmp_home fake_bin state_dir status

  tmp_home="$(new_home)"
  fake_bin="$tmp_home/fake-bin"
  state_dir="$tmp_home/bytecode-guard-state"
  setup_bytecode_guard_python3 "$fake_bin/python3"

  set +e
  HOME="$tmp_home" \
    PATH="$fake_bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=0 \
    TMP_BYTECODE_GUARD_STATE="$state_dir" \
    bash "$SNAPSHOT_SCRIPT" > "$tmp_home/snapshot.out" 2> "$tmp_home/snapshot.err"
  status=$?
  set -e

  if [ "$status" -ne 0 ]; then
    cat "$tmp_home/snapshot.err" >&2
    cleanup_home "$tmp_home"
    return "$status"
  fi

  set +e
  HOME="$tmp_home" \
    PATH="$fake_bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=0 \
    TMP_BYTECODE_GUARD_STATE="$state_dir" \
    bash "$SNAPSHOT_SCRIPT" --snapshot-lock-held \
      >> "$tmp_home/snapshot.out" 2>> "$tmp_home/snapshot.err"
  status=$?
  set -e

  if [ "$status" -ne 0 ]; then
    cat "$tmp_home/snapshot.err" >&2
    cleanup_home "$tmp_home"
    return "$status"
  fi

  assert_contains "$state_dir/python3.log" \
    "PYTHONDONTWRITEBYTECODE=1 $SNAPSHOT_LOCK_HELPER acquire"
  assert_contains "$state_dir/python3.log" \
    "PYTHONDONTWRITEBYTECODE=1 $SNAPSHOT_LOCK_HELPER verify"
  assert_not_contains "$state_dir/python3.log" "codex_repair_rollout_reflinks.py"

  cleanup_home "$tmp_home"
}

test_snapshot_archives_complete_source_trees_without_touching_mirror() {
  local tmp_home source_root sessions_root archived_root archive_path mirror_file expected_mirror
  local expected_opaque expected_partial expected_special special_rel link_listing

  tmp_home="$(new_home)"
  source_root="$tmp_home/.codex"
  sessions_root="$source_root/sessions"
  archived_root="$source_root/archived_sessions"
  archive_path="$(snapshot_archive_path "$tmp_home")"
  mirror_file="$tmp_home/.dotfiles/codex-backup/mirror/sessions/legacy/retained.bin"
  expected_mirror="$tmp_home/expected-legacy-mirror.bin"
  expected_opaque="$tmp_home/expected-opaque.zst"
  expected_partial="$tmp_home/expected-partial.jsonl"
  expected_special="$tmp_home/expected-special-name.bin"
  special_rel=$'sessions/special\tline\nback\\slash'

  mkdir -p "$sessions_root/.hidden-dir" "$sessions_root/nested/empty" \
    "$archived_root/old/empty" "$(dirname "$mirror_file")"
  printf '\000ZSTD\377opaque\000bytes' > "$sessions_root/archive.zst"
  printf '\n{"partial":true' > "$sessions_root/arbitrary-name"
  printf '\n{"partial":true' > "$expected_partial"
  printf '\000ZSTD\377opaque\000bytes' > "$expected_opaque"
  printf 'special-name payload\000' > "$sessions_root/${special_rel#sessions/}"
  cp "$sessions_root/${special_rel#sessions/}" "$expected_special"
  : > "$sessions_root/.hidden-dir/.hidden-file"
  printf 'no extension\000payload' > "$archived_root/no-extension"
  printf 'legacy mirror sentinel\000' > "$mirror_file"
  cp "$mirror_file" "$expected_mirror"
  ln -s "$tmp_home/outside-target" "$archived_root/child-link"
  printf 'must not be archived through symlink\n' > "$tmp_home/outside-target"

  HOME="$tmp_home" bash "$SNAPSHOT_SCRIPT"

  assert_file_exists "$archive_path"
  assert_archive_path_exists "$archive_path" "sessions/nested/empty"
  assert_archive_path_exists "$archive_path" "archived_sessions/old/empty"
  assert_archive_file_equals "$archive_path" "sessions/archive.zst" "$expected_opaque"
  assert_archive_file_equals "$archive_path" "sessions/arbitrary-name" "$expected_partial"
  assert_archive_file_equals "$archive_path" "$special_rel" "$expected_special"
  assert_archive_file_equals "$archive_path" "sessions/.hidden-dir/.hidden-file" \
    "$sessions_root/.hidden-dir/.hidden-file"
  assert_archive_path_exists "$archive_path" "archived_sessions/no-extension"
  assert_archive_path_exists "$archive_path" "archived_sessions/child-link"
  assert_archive_not_contains "$archive_path" "archived_sessions/outside-target"
  link_listing="$(snapshot_archive_verbose_list "$archive_path")"
  if ! printf '%s\n' "$link_listing" | grep -E '^l[^[:space:]]*[[:space:]].* archived_sessions/child-link( -> .*)?$' >/dev/null; then
    printf 'Expected child symlink to be stored as a symlink in the archive\n' >&2
    exit 1
  fi
  assert_archive_not_contains "$archive_path" "sessions/../../outside-target"
  assert_file_exists "$mirror_file"
  assert_contains "$mirror_file" "legacy mirror sentinel"
  assert_archive_not_contains "$archive_path" "sessions/legacy/retained.bin"
  cmp -s "$expected_mirror" "$mirror_file"

  cleanup_home "$tmp_home"
}

test_snapshot_archives_one_existing_source_tree() {
  local tmp_home archive_path archived_file

  tmp_home="$(new_home)"
  archived_file="$tmp_home/.codex/archived_sessions/one-sided/rollout.zst"
  mkdir -p "$(dirname "$archived_file")"
  printf '\000opaque archived bytes\377' > "$archived_file"

  HOME="$tmp_home" bash "$SNAPSHOT_SCRIPT"

  archive_path="$(snapshot_archive_path "$tmp_home")"
  assert_file_exists "$archive_path"
  assert_archive_file_equals "$archive_path" "archived_sessions/one-sided/rollout.zst" "$archived_file"
  assert_archive_not_contains "$archive_path" "sessions"

  cleanup_home "$tmp_home"
}

test_snapshot_skips_when_both_source_trees_are_missing() {
  local tmp_home archive_path log_path old_mirror

  tmp_home="$(new_home)"
  archive_path="$(snapshot_archive_path "$tmp_home")"
  log_path="$tmp_home/Library/Logs/codex_snapshot_daily.log"
  old_mirror="$tmp_home/.dotfiles/codex-backup/mirror/sessions/legacy/retained.bin"
  mkdir -p "$tmp_home/.codex"
  mkdir -p "$(dirname "$old_mirror")"
  printf 'legacy mirror must not be used\n' > "$old_mirror"
  HOME="$tmp_home" bash "$SNAPSHOT_SCRIPT"

  assert_contains "$log_path" "Source rollout directories missing, skipping snapshot."
  assert_not_exists "$archive_path"
  assert_contains "$old_mirror" "legacy mirror must not be used"

  cleanup_home "$tmp_home"
}

test_snapshot_does_not_publish_when_tar_fails() {
  local tmp_home source_file fake_bin marker archive_path expected_archive status

  tmp_home="$(new_home)"
  source_file="$tmp_home/.codex/sessions/opaque-file"
  fake_bin="$tmp_home/fake-bin"
  marker="$tmp_home/tar-failed.marker"
  archive_path="$(snapshot_archive_path "$tmp_home")"
  expected_archive="$tmp_home/expected-existing-archive"
  mkdir -p "$(dirname "$source_file")" "$fake_bin"
  printf 'opaque source bytes' > "$source_file"
  mkdir -p "$(dirname "$archive_path")"
  printf 'previous complete archive\000sentinel' > "$archive_path"
  cp "$archive_path" "$expected_archive"
  setup_forbidden_tar "$fake_bin/tar"

  set +e
  HOME="$tmp_home" PATH="$fake_bin:$PATH" TMP_FORBIDDEN_TAR_MARKER="$marker" \
    bash "$SNAPSHOT_SCRIPT" > "$tmp_home/tar-failure.out" 2> "$tmp_home/tar-failure.err"
  status=$?
  set -e

  if [ "$status" -eq 0 ]; then
    printf 'Expected tar failure to stop snapshot publication\n' >&2
    exit 1
  fi
  assert_file_exists "$marker"
  cmp -s "$expected_archive" "$archive_path"
  if find "$tmp_home/.dotfiles/codex-backup/state/snapshot-tmp" \
    -maxdepth 1 -type f -name '*.tmp.*' -print | grep -q .; then
    printf 'Expected failed tar staging output to be cleaned\n' >&2
    exit 1
  fi

  cleanup_home "$tmp_home"
}

test_snapshot_does_not_publish_when_zstd_fails() {
  local tmp_home source_file fake_bin archive_path staging_dir status

  tmp_home="$(new_home)"
  source_file="$tmp_home/.codex/sessions/opaque-file"
  fake_bin="$tmp_home/fake-bin"
  archive_path="$tmp_home/OneDrive/Backup/dotfiles/codex/$TEST_DEFAULT_COMPUTER_NAME/snapshots/codex-rollouts-$(date +%Y-%m-%d).tar.zst"
  staging_dir="$tmp_home/.dotfiles/codex-backup/state/snapshot-tmp"
  mkdir -p "$(dirname "$source_file")" "$fake_bin"
  printf 'opaque source bytes' > "$source_file"
  setup_partial_failure_zstd "$fake_bin/zstd"

  set +e
  HOME="$tmp_home" PATH="$fake_bin:$PATH" bash "$SNAPSHOT_SCRIPT" \
    > "$tmp_home/zstd-failure.out" 2> "$tmp_home/zstd-failure.err"
  status=$?
  set -e

  if [ "$status" -eq 0 ]; then
    printf 'Expected zstd failure to stop snapshot publication\n' >&2
    exit 1
  fi
  assert_not_exists "$archive_path"
  if find "$staging_dir" -maxdepth 1 -type f -name '*.tmp.*' -print | grep -q .; then
    printf 'Expected failed zstd staging output to be cleaned\n' >&2
    exit 1
  fi

  cleanup_home "$tmp_home"
}

test_snapshot_lock_rejects_unsafe_lock_leaves() {
  local tmp_home symlink_state hardlink_state fifo_state wrong_type_state
  local target status target_mode

  tmp_home="$(new_home)"

  symlink_state="$tmp_home/symlink-state"
  target="$tmp_home/symlink-target"
  mkdir -p "$symlink_state"
  printf 'symlink target\n' > "$target"
  chmod 640 "$target"
  ln -s "$target" "$symlink_state/snapshot.lock"
  status="$(run_with_deadline \
    "$tmp_home/symlink.status" \
    /usr/bin/env HOME="$tmp_home" python3 "$SNAPSHOT_LOCK_HELPER" acquire \
      --state-root "$symlink_state" --script "$SNAPSHOT_SCRIPT" \
      --log "$tmp_home/symlink.log" \
    2> "$tmp_home/symlink.err")"
  if [ "$status" -eq 0 ]; then
    printf 'Expected snapshot lock helper to reject a symlink leaf\n' >&2
    exit 1
  fi
  target_mode="$(/usr/bin/stat -f%Lp "$target")"
  if [ "$target_mode" != "640" ]; then
    printf 'Expected symlink target mode to remain 0640, got %s\n' "$target_mode" >&2
    exit 1
  fi

  hardlink_state="$tmp_home/hardlink-state"
  target="$tmp_home/hardlink-target"
  mkdir -p "$hardlink_state"
  printf 'hardlink target\n' > "$target"
  chmod 640 "$target"
  ln "$target" "$hardlink_state/snapshot.lock"
  status="$(run_with_deadline \
    "$tmp_home/hardlink.status" \
    /usr/bin/env HOME="$tmp_home" python3 "$SNAPSHOT_LOCK_HELPER" acquire \
      --state-root "$hardlink_state" --script "$SNAPSHOT_SCRIPT" \
      --log "$tmp_home/hardlink.log" \
    2> "$tmp_home/hardlink.err")"
  if [ "$status" -eq 0 ]; then
    printf 'Expected snapshot lock helper to reject a hard-linked leaf\n' >&2
    exit 1
  fi
  target_mode="$(/usr/bin/stat -f%Lp "$target")"
  if [ "$target_mode" != "640" ]; then
    printf 'Expected hardlink target mode to remain 0640, got %s\n' "$target_mode" >&2
    exit 1
  fi

  fifo_state="$tmp_home/fifo-state"
  mkdir -p "$fifo_state"
  mkfifo "$fifo_state/snapshot.lock"
  status="$(run_with_deadline \
    "$tmp_home/fifo.status" \
    /usr/bin/env HOME="$tmp_home" python3 "$SNAPSHOT_LOCK_HELPER" acquire \
      --state-root "$fifo_state" --script "$SNAPSHOT_SCRIPT" \
      --log "$tmp_home/fifo.log" \
    2> "$tmp_home/fifo.err")"
  if [ "$status" -eq 0 ]; then
    printf 'Expected snapshot lock helper to reject a FIFO leaf\n' >&2
    exit 1
  fi
  if [ ! -p "$fifo_state/snapshot.lock" ]; then
    printf 'Expected rejected snapshot lock FIFO to remain unchanged\n' >&2
    exit 1
  fi

  wrong_type_state="$tmp_home/wrong-type-state"
  mkdir -p "$wrong_type_state/snapshot.lock"
  status="$(run_with_deadline \
    "$tmp_home/wrong-type.status" \
    /usr/bin/env HOME="$tmp_home" python3 "$SNAPSHOT_LOCK_HELPER" acquire \
      --state-root "$wrong_type_state" --script "$SNAPSHOT_SCRIPT" \
      --log "$tmp_home/wrong-type.log" \
    2> "$tmp_home/wrong-type.err")"
  if [ "$status" -eq 0 ]; then
    printf 'Expected snapshot lock helper to reject a directory lock leaf\n' >&2
    exit 1
  fi
  if [ ! -d "$wrong_type_state/snapshot.lock" ]; then
    printf 'Expected rejected directory lock leaf to remain unchanged\n' >&2
    exit 1
  fi

  cleanup_home "$tmp_home"
}

test_snapshot_lock_detects_path_replacement() {
  local tmp_home state_root hook_path hook_state replacement_stage status

  tmp_home="$(new_home)"
  hook_path="$tmp_home/fake-bin/replace-lock-name"
  setup_lock_replacement_hook "$hook_path"

  for replacement_stage in before_flock after_flock; do
    state_root="$tmp_home/replacement-$replacement_stage-state"
    hook_state="$tmp_home/replacement-$replacement_stage-hook-state"
    mkdir -p "$state_root"
    status="$(run_with_deadline \
      "$tmp_home/replacement-$replacement_stage.status" \
      /usr/bin/env \
        HOME="$tmp_home" \
        CODEX_SNAPSHOT_LOCK_TEST_HOOK="$hook_path" \
        TMP_LOCK_REPLACEMENT_STATE="$hook_state" \
        TMP_LOCK_REPLACEMENT_STAGE="$replacement_stage" \
        python3 "$SNAPSHOT_LOCK_HELPER" acquire \
          --state-root "$state_root" --script "$SNAPSHOT_SCRIPT" \
          --log "$tmp_home/replacement-$replacement_stage.log" \
      2> "$tmp_home/replacement-$replacement_stage.err")"
    if [ "$status" -eq 0 ]; then
      printf 'Expected snapshot lock helper to reject replacement at %s\n' "$replacement_stage" >&2
      exit 1
    fi
    assert_file_exists "$state_root/snapshot.lock"
    assert_file_exists "$state_root/snapshot.lock.opened"
    assert_contains "$tmp_home/replacement-$replacement_stage.err" "no longer maps to the opened lock inode"
  done

  cleanup_home "$tmp_home"
}

test_snapshot_lock_detects_state_ancestor_replacement() {
  local tmp_home case_home state_root ancestor hook_path hook_state
  local replacement_stage archive_path status

  tmp_home="$(new_home)"
  hook_path="$tmp_home/fake-bin/replace-state-ancestor"
  setup_state_root_replacement_hook "$hook_path"

  for replacement_stage in \
    before_flock \
    after_flock \
    verify_before_flock \
    verify_after_flock; do
    case_home="$tmp_home/case-$replacement_stage"
    ancestor="$case_home/lock-ancestor"
    state_root="$ancestor/state"
    hook_state="$tmp_home/state-replacement-$replacement_stage-hook"
    archive_path="$(snapshot_archive_path "$case_home")"
    mkdir -p \
      "$case_home/Library/Logs" \
      "$case_home/.codex/sessions/day" \
      "$state_root"
    printf '{"step":1}\n' > "$case_home/.codex/sessions/day/rollout-state-replacement.jsonl"

    set +e
    HOME="$case_home" \
      CODEX_BACKUP_STATE_ROOT="$state_root" \
      CODEX_SNAPSHOT_LOCK_TEST_HOOK="$hook_path" \
      TMP_STATE_ROOT_REPLACEMENT_STATE="$hook_state" \
      TMP_STATE_ROOT_REPLACEMENT_STAGE="$replacement_stage" \
      TMP_STATE_ROOT_REPLACEMENT_ANCESTOR="$ancestor" \
      bash "$SNAPSHOT_SCRIPT" \
        > "$case_home/snapshot.out" \
        2> "$case_home/snapshot.err"
    status=$?
    set -e

    if [ "$status" -eq 0 ]; then
      printf 'Expected state ancestor replacement at %s to fail\n' "$replacement_stage" >&2
      exit 1
    fi
    assert_not_exists "$archive_path"
    assert_contains "$case_home/snapshot.err" "canonical state-root path no longer maps to the held directory"
  done

  cleanup_home "$tmp_home"
}

test_snapshot_lock_rejects_concurrent_zstd_run() {
  local tmp_home fake_bin zstd_state real_zstd src_file archive_path staging_dir
  local state_root lock_path log_path first_pid first_status second_status zstd_tmp_count
  local lock_mode lock_links state_mode lock_identity_before lock_identity_after

  real_zstd="$(command -v zstd 2>/dev/null || true)"
  [ -n "$real_zstd" ] || return 0

  tmp_home="$(new_home)"
  fake_bin="$tmp_home/fake-bin"
  zstd_state="$tmp_home/blocking-zstd-state"
  src_file="$tmp_home/.codex/sessions/day/rollout-lock.jsonl"
  archive_path="$(snapshot_archive_path "$tmp_home")"
  staging_dir="$tmp_home/snapshot-staging"
  state_root="$tmp_home/backup-state"
  lock_path="$state_root/snapshot.lock"
  log_path="$tmp_home/Library/Logs/codex_snapshot_daily.log"

  mkdir -p "$(dirname "$src_file")"
  setup_blocking_zstd "$fake_bin/zstd"
  printf '{"step":1}\n' > "$src_file"

  HOME="$tmp_home" \
    PATH="$fake_bin:$PATH" \
    CODEX_BACKUP_STATE_ROOT="$state_root" \
    CODEX_SNAPSHOT_STAGING_DIR="$staging_dir" \
    TMP_BLOCKING_ZSTD_STATE="$zstd_state" \
    TMP_REAL_ZSTD="$real_zstd" \
    bash "$SNAPSHOT_SCRIPT" > "$tmp_home/first.out" 2> "$tmp_home/first.err" &
  first_pid=$!

  if ! wait_for_file "$zstd_state/entered"; then
    : > "$zstd_state/release"
    wait "$first_pid" 2>/dev/null || true
    printf 'Timed out waiting for first snapshot to create its zstd temp\n' >&2
    exit 1
  fi

  zstd_tmp_count="$(find "$staging_dir" -maxdepth 1 -type f -name 'codex-rollouts-*.tar.zst.tmp.*' -print | wc -l | tr -d ' ')"
  if [ "$zstd_tmp_count" -ne 1 ]; then
    : > "$zstd_state/release"
    wait "$first_pid" 2>/dev/null || true
    printf 'Expected one current zstd temp while the first run holds the lock, got %s\n' "$zstd_tmp_count" >&2
    exit 1
  fi
  lock_mode="$(/usr/bin/stat -f%Lp "$lock_path")"
  lock_links="$(/usr/bin/stat -f%l "$lock_path")"
  state_mode="$(/usr/bin/stat -f%Lp "$state_root")"
  lock_identity_before="$(/usr/bin/stat -f '%d:%i' "$lock_path")"

  set +e
  HOME="$tmp_home" \
    PATH="$fake_bin:$PATH" \
    CODEX_BACKUP_STATE_ROOT="$state_root" \
    CODEX_SNAPSHOT_STAGING_DIR="$staging_dir" \
    TMP_BLOCKING_ZSTD_STATE="$zstd_state" \
    TMP_REAL_ZSTD="$real_zstd" \
    bash "$SNAPSHOT_SCRIPT" > "$tmp_home/second.out" 2> "$tmp_home/second.err"
  second_status=$?
  set -e

  zstd_tmp_count="$(find "$staging_dir" -maxdepth 1 -type f -name 'codex-rollouts-*.tar.zst.tmp.*' -print | wc -l | tr -d ' ')"
  : > "$zstd_state/release"
  set +e
  wait "$first_pid"
  first_status=$?
  set -e

  if [ "$second_status" -ne 75 ]; then
    printf 'Expected concurrent snapshot to fail fast with 75, got %s\n' "$second_status" >&2
    exit 1
  fi
  if [ "$zstd_tmp_count" -ne 1 ]; then
    printf 'Expected concurrent rejection to leave exactly one zstd temp, got %s\n' "$zstd_tmp_count" >&2
    exit 1
  fi
  if [ "$first_status" -ne 0 ]; then
    printf 'Expected first snapshot to finish after zstd release, got %s\n' "$first_status" >&2
    exit 1
  fi
  if [ "$lock_mode" != "600" ] || [ "$lock_links" -ne 1 ] || [ "$state_mode" != "700" ]; then
    printf 'Unexpected private lock policy: state=%s lock=%s links=%s\n' "$state_mode" "$lock_mode" "$lock_links" >&2
    exit 1
  fi
  if [ ! -f "$lock_path" ] || [ -L "$lock_path" ]; then
    printf 'Expected snapshot.lock to remain a non-symlink regular file\n' >&2
    exit 1
  fi
  assert_no_extended_acl "$state_root"
  assert_no_extended_acl "$lock_path"
  assert_contains "$log_path" "Snapshot already running; lock unavailable"
  assert_file_exists "$archive_path"
  assert_archive_contains "$archive_path" "sessions/day/rollout-lock.jsonl"

  HOME="$tmp_home" \
    PATH="$fake_bin:$PATH" \
    CODEX_BACKUP_STATE_ROOT="$state_root" \
    CODEX_SNAPSHOT_STAGING_DIR="$staging_dir" \
    TMP_BLOCKING_ZSTD_STATE="$zstd_state" \
    TMP_REAL_ZSTD="$real_zstd" \
    bash "$SNAPSHOT_SCRIPT"

  lock_identity_after="$(/usr/bin/stat -f '%d:%i' "$lock_path")"
  if [ "$lock_identity_before" != "$lock_identity_after" ]; then
    printf 'Expected persistent snapshot lock inode to remain stable\n' >&2
    exit 1
  fi

  cleanup_home "$tmp_home"
}

test_sync_safe_helper_uses_apfs_reflink_and_absent_private_stage() {
  local tmp_home state_dir source_path mirror_path hook_path stage_path
  local log_path source_mode mirror_mode source_mtime mirror_mtime source_xattr mirror_xattr

  tmp_home="$(new_home)"
  state_dir="$tmp_home/mirror-helper-observer"
  source_path="$tmp_home/.codex/sessions/day/rollout-safe-reflink.jsonl"
  mirror_path="$tmp_home/.dotfiles/codex-backup/mirror/sessions/day/rollout-safe-reflink.jsonl"
  hook_path="$tmp_home/fake-bin/observe-safe-mirror-helper"
  log_path="$tmp_home/sync-strict-reflink.log"

  mkdir -p "$(dirname "$source_path")"
  setup_mirror_helper_observer_hook "$hook_path"
  printf '{"step":1}\n' > "$source_path"
  chmod 640 "$source_path"
  touch -t 202001020304.05 "$source_path"
  /usr/bin/xattr -w com.openai.codex.test mirror-source-metadata "$source_path"
  : > "$log_path"

  HOME="$tmp_home" \
    LOG="$log_path" \
    CODEX_ROLLOUT_MIRROR_TEST_HOOK="$hook_path" \
    TMP_MIRROR_OBSERVER_SOURCE="$source_path" \
    TMP_MIRROR_OBSERVER_STATE="$state_dir" \
    run_mirror_sync_in_isolated_shell

  assert_file_exists "$mirror_path"
  assert_contains "$log_path" "Mirror update sessions/day/rollout-safe-reflink.jsonl (0 -> 11, method=reflink)"
  assert_contains "$state_dir/stages.log" "after_stage_create"
  assert_contains "$state_dir/stages.log" "after_clone"
  assert_contains "$state_dir/stages.log" "before_publish"
  assert_contains "$state_dir/stages.log" "after_publish"
  stage_path="$(cat "$state_dir/stage.path")"
  assert_not_exists "$stage_path"
  if ! cmp -s "$source_path" "$mirror_path"; then
    printf 'Expected safe reflink result to match source: %s\n' "$mirror_path" >&2
    exit 1
  fi

  source_mode="$(/usr/bin/stat -f%Lp "$source_path")"
  mirror_mode="$(/usr/bin/stat -f%Lp "$mirror_path")"
  source_mtime="$(/usr/bin/stat -f%m "$source_path")"
  mirror_mtime="$(/usr/bin/stat -f%m "$mirror_path")"
  if [ "$source_mode" != "$mirror_mode" ] || [ "$source_mtime" != "$mirror_mtime" ]; then
    printf 'Expected safe reflink copy to preserve mode and mtime\n' >&2
    exit 1
  fi
  source_xattr="$(/usr/bin/xattr -p com.openai.codex.test "$source_path")"
  mirror_xattr="$(/usr/bin/xattr -p com.openai.codex.test "$mirror_path")"
  if [ "$source_xattr" != "$mirror_xattr" ]; then
    printf 'Expected safe reflink copy to preserve source xattrs\n' >&2
    exit 1
  fi

  cleanup_home "$tmp_home"
}

test_sync_safe_helper_rejects_inherited_destination_acl() {
  local tmp_home source_path mirror_path mirror_parent expected_mirror log_path status
  local mirror_mode mirror_mtime expected_mode expected_mtime

  tmp_home="$(new_home)"
  source_path="$tmp_home/.codex/sessions/day/rollout-unsafe-container.jsonl"
  mirror_path="$tmp_home/.dotfiles/codex-backup/mirror/sessions/day/rollout-unsafe-container.jsonl"
  mirror_parent="$(dirname "$mirror_path")"
  expected_mirror="$tmp_home/expected-unsafe-container-mirror.jsonl"
  log_path="$tmp_home/sync-unsafe-container.log"

  mkdir -p "$(dirname "$source_path")" "$mirror_parent"
  printf '{"source":"new and longer"}\n' > "$source_path"
  printf '{"mirror":"old"}\n' > "$mirror_path"
  chmod 640 "$mirror_path"
  touch -t 202002030405.06 "$mirror_path"
  cp -p "$mirror_path" "$expected_mirror"
  expected_mode="$(/usr/bin/stat -f%Lp "$mirror_path")"
  expected_mtime="$(/usr/bin/stat -f%m "$mirror_path")"
  /bin/chmod +a \
    "everyone allow list,search,readattr,readextattr,readsecurity,directory_inherit" \
    "$mirror_parent"
  : > "$log_path"

  set +e
  HOME="$tmp_home" LOG="$log_path" run_mirror_sync_in_isolated_shell
  status=$?
  set -e

  if [ "$status" -ne 2 ]; then
    printf 'Expected unsafe destination container to fail closed with 2, got %s\n' "$status" >&2
    exit 1
  fi
  cmp -s "$expected_mirror" "$mirror_path"
  mirror_mode="$(/usr/bin/stat -f%Lp "$mirror_path")"
  mirror_mtime="$(/usr/bin/stat -f%m "$mirror_path")"
  if [ "$mirror_mode" != "$expected_mode" ] || [ "$mirror_mtime" != "$expected_mtime" ]; then
    printf 'Expected rejected unsafe-container mirror metadata to remain unchanged\n' >&2
    exit 1
  fi
  assert_contains "$log_path" "Safe mirror sync failed: sessions/day/rollout-unsafe-container.jsonl"
  if find "$mirror_parent" -mindepth 1 -maxdepth 1 -type d -print | grep -q .; then
    printf 'Did not expect staging directories after unsafe-container rejection\n' >&2
    exit 1
  fi

  /bin/chmod -N "$mirror_parent"
  cleanup_home "$tmp_home"
}

test_sync_requires_nonempty_log_before_touching_mirror_state() {
  local tmp_home source_path mirror_path state_root state_sentinel hook_path hook_marker
  local tmp_dir expected_source expected_mirror expected_state status variant
  local source_identity mirror_identity state_identity source_mtime mirror_mtime state_mtime
  local source_mode mirror_mode state_mode

  tmp_home="$(new_home)"
  source_path="$tmp_home/.codex/sessions/day/rollout-log-required.jsonl"
  mirror_path="$tmp_home/custom-mirror/sessions/day/rollout-log-required.jsonl"
  state_root="$tmp_home/custom-state"
  state_sentinel="$state_root/sentinel"
  hook_path="$tmp_home/fake-bin/forbidden-mirror-hook"
  hook_marker="$tmp_home/forbidden-mirror-hook.called"
  tmp_dir="$tmp_home/tmp"
  expected_source="$tmp_home/expected-log-source.jsonl"
  expected_mirror="$tmp_home/expected-log-mirror.jsonl"
  expected_state="$tmp_home/expected-log-state"

  mkdir -p "$(dirname "$source_path")" "$(dirname "$mirror_path")" "$state_root" "$tmp_dir"
  setup_forbidden_mirror_hook "$hook_path"
  printf '{"source":"unchanged"}\n' > "$source_path"
  printf '{"mirror":"unchanged"}\n' > "$mirror_path"
  printf 'state unchanged\n' > "$state_sentinel"
  cp -p "$source_path" "$expected_source"
  cp -p "$mirror_path" "$expected_mirror"
  cp -p "$state_sentinel" "$expected_state"
  source_identity="$(/usr/bin/stat -f '%d:%i' "$source_path")"
  mirror_identity="$(/usr/bin/stat -f '%d:%i' "$mirror_path")"
  state_identity="$(/usr/bin/stat -f '%d:%i' "$state_sentinel")"
  source_mtime="$(/usr/bin/stat -f%m "$source_path")"
  mirror_mtime="$(/usr/bin/stat -f%m "$mirror_path")"
  state_mtime="$(/usr/bin/stat -f%m "$state_sentinel")"
  source_mode="$(/usr/bin/stat -f%Lp "$source_path")"
  mirror_mode="$(/usr/bin/stat -f%Lp "$mirror_path")"
  state_mode="$(/usr/bin/stat -f%Lp "$state_sentinel")"

  for variant in unset empty; do
    set +e
    if [ "$variant" = "unset" ]; then
      HOME="$tmp_home" \
        TMPDIR="$tmp_dir" \
        CODEX_ROOT="$tmp_home/.codex" \
        CODEX_MIRROR_ROOT="$tmp_home/custom-mirror" \
        CODEX_BACKUP_STATE_ROOT="$state_root" \
        CODEX_ROLLOUT_MIRROR_TEST_HOOK="$hook_path" \
        TMP_FORBIDDEN_MIRROR_HOOK_MARKER="$hook_marker" \
        run_mirror_sync_without_log_in_isolated_shell \
        2> "$tmp_home/log-$variant.err"
      status=$?
    else
      HOME="$tmp_home" \
        TMPDIR="$tmp_dir" \
        CODEX_ROOT="$tmp_home/.codex" \
        CODEX_MIRROR_ROOT="$tmp_home/custom-mirror" \
        CODEX_BACKUP_STATE_ROOT="$state_root" \
        CODEX_ROLLOUT_MIRROR_TEST_HOOK="$hook_path" \
        TMP_FORBIDDEN_MIRROR_HOOK_MARKER="$hook_marker" \
        LOG="" \
        run_mirror_sync_in_isolated_shell \
        2> "$tmp_home/log-$variant.err"
      status=$?
    fi
    set -e

    if [ "$status" -ne 2 ]; then
      printf 'Expected %s LOG mirror sync to fail immediately with 2, got %s\n' "$variant" "$status" >&2
      exit 1
    fi
    assert_contains "$tmp_home/log-$variant.err" "LOG must be set before syncing the Codex rollout mirror."
    assert_not_exists "$hook_marker"
    cmp -s "$expected_source" "$source_path"
    cmp -s "$expected_mirror" "$mirror_path"
    cmp -s "$expected_state" "$state_sentinel"
    if [ "$(/usr/bin/stat -f '%d:%i' "$source_path")" != "$source_identity" ] || \
      [ "$(/usr/bin/stat -f '%d:%i' "$mirror_path")" != "$mirror_identity" ] || \
      [ "$(/usr/bin/stat -f '%d:%i' "$state_sentinel")" != "$state_identity" ] || \
      [ "$(/usr/bin/stat -f%m "$source_path")" != "$source_mtime" ] || \
      [ "$(/usr/bin/stat -f%m "$mirror_path")" != "$mirror_mtime" ] || \
      [ "$(/usr/bin/stat -f%m "$state_sentinel")" != "$state_mtime" ] || \
      [ "$(/usr/bin/stat -f%Lp "$source_path")" != "$source_mode" ] || \
      [ "$(/usr/bin/stat -f%Lp "$mirror_path")" != "$mirror_mode" ] || \
      [ "$(/usr/bin/stat -f%Lp "$state_sentinel")" != "$state_mode" ]; then
      printf 'Expected %s LOG rejection to preserve source, mirror, and state identity/metadata\n' "$variant" >&2
      exit 1
    fi
    if find "$tmp_dir" -mindepth 1 -maxdepth 1 -print | grep -q .; then
      printf 'Did not expect temp files before %s LOG rejection\n' "$variant" >&2
      exit 1
    fi
  done

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

test_snapshot_archives_empty_source() {
  local tmp_home log_path archive_path

  tmp_home="$(new_home)"
  mkdir -p "$tmp_home/.codex/sessions/empty"
  log_path="$tmp_home/Library/Logs/codex_snapshot_daily.log"

  HOME="$tmp_home" bash "$SNAPSHOT_SCRIPT"

  archive_path="$(snapshot_archive_path "$tmp_home")"
  assert_file_exists "$log_path"
  assert_file_exists "$archive_path"
  assert_archive_path_exists "$archive_path" "sessions/empty"
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

test_snapshot_default_paths_follow_runtime_computer_name() {
  local tmp_home src_file first_archive second_archive fake_bin computer_name_path

  tmp_home="$(new_home)"
  fake_bin="$tmp_home/fake-bin"
  computer_name_path="$tmp_home/computer-name"
  src_file="$tmp_home/.codex/sessions/day/rollout-computer-path.jsonl"
  mkdir -p "$fake_bin" "$(dirname "$src_file")"
  cat > "$fake_bin/scutil" <<'EOF'
#!/bin/bash
set -euo pipefail
[ "${1:-}" = "--get" ] && [ "${2:-}" = "ComputerName" ]
cat "$TMP_FAKE_SCUTIL_NAME"
EOF
  chmod +x "$fake_bin/scutil"
  printf '{"step":1}\n' > "$src_file"

  printf 'MacBook Air\n' > "$computer_name_path"
  HOME="$tmp_home" PATH="$fake_bin:$PATH" TMP_FAKE_SCUTIL_NAME="$computer_name_path" \
    env -u CODEX_BACKUP_COMPUTER_NAME bash "$SNAPSHOT_SCRIPT"
  first_archive="$(snapshot_archive_path "$tmp_home" "MacBook Air")"
  assert_file_exists "$first_archive"

  printf 'MacBook Air (2) — Joey\n' > "$computer_name_path"
  HOME="$tmp_home" PATH="$fake_bin:$PATH" TMP_FAKE_SCUTIL_NAME="$computer_name_path" \
    env -u CODEX_BACKUP_COMPUTER_NAME bash "$SNAPSHOT_SCRIPT"
  second_archive="$(snapshot_archive_path "$tmp_home" "MacBook Air (2) — Joey")"
  assert_file_exists "$second_archive"
  assert_not_exists "$tmp_home/OneDrive/Backup/dotfiles/codex/snapshots"

  cleanup_home "$tmp_home"
}

test_snapshot_explicit_directory_does_not_require_computer_name() {
  local tmp_home fake_bin src_file archive_path

  tmp_home="$(new_home)"
  fake_bin="$tmp_home/fake-bin"
  src_file="$tmp_home/.codex/sessions/day/rollout-explicit-snapshot-dir.jsonl"
  archive_path="$tmp_home/Custom Snapshots/codex-rollouts-$(date +%Y-%m-%d).tar"
  if command -v zstd >/dev/null 2>&1; then
    archive_path="$archive_path.zst"
  else
    archive_path="$archive_path.gz"
  fi
  mkdir -p "$fake_bin" "$(dirname "$src_file")"
  cat > "$fake_bin/scutil" <<'EOF'
#!/bin/bash
exit 1
EOF
  chmod +x "$fake_bin/scutil"
  printf '{"step":1}\n' > "$src_file"

  HOME="$tmp_home" \
    PATH="$fake_bin:$PATH" \
    CODEX_SNAPSHOT_DIR="$tmp_home/Custom Snapshots" \
    env -u CODEX_BACKUP_COMPUTER_NAME bash "$SNAPSHOT_SCRIPT"

  assert_file_exists "$archive_path"
  cleanup_home "$tmp_home"
}

test_snapshot_fails_closed_without_valid_computer_name() {
  local tmp_home fake_bin status invalid_name
  local invalid_names=("Bad/Name" 'Bad\Name' '..' 'Bad:' 'Bad*Name' 'Bad?Name' 'Bad"Name' 'Bad<Name' 'Bad>Name' 'Bad|Name' 'Trailing.' 'Trailing ' ' Leading' '.lock' 'CON' 'PRN' 'AUX' 'NUL' 'CON.txt' 'PRN.backup' 'AUX.1' 'NUL.log' 'cOn.TXT' 'pRn.Backup' 'aUx.1' 'nUl.log' 'COM0' 'COM9' 'LPT0' 'LPT9' 'desktop.ini' '~$lock' 'Mac_vti_Name' $'Bad\nName')

  tmp_home="$(new_home)"
  fake_bin="$tmp_home/fake-bin"
  mkdir -p "$fake_bin"
  cat > "$fake_bin/scutil" <<'EOF'
#!/bin/bash
exit 1
EOF
  chmod +x "$fake_bin/scutil"

  set +e
  HOME="$tmp_home" PATH="$fake_bin:$PATH" env -u CODEX_BACKUP_COMPUTER_NAME bash "$SNAPSHOT_SCRIPT" \
    > "$tmp_home/no-name.out" 2> "$tmp_home/no-name.err"
  status=$?
  set -e
  if [ "$status" -eq 0 ]; then
    printf 'Expected snapshot to fail when scutil cannot read ComputerName\n' >&2
    exit 1
  fi
  assert_contains "$tmp_home/no-name.err" "Unable to read macOS ComputerName with scutil"
  assert_not_exists "$tmp_home/OneDrive/Backup/dotfiles/codex"

  for invalid_name in "${invalid_names[@]}"; do
    set +e
    HOME="$tmp_home" CODEX_BACKUP_COMPUTER_NAME="$invalid_name" bash "$SNAPSHOT_SCRIPT" \
      > "$tmp_home/invalid-name.out" 2> "$tmp_home/invalid-name.err"
    status=$?
    set -e
    if [ "$status" -eq 0 ]; then
      printf 'Expected snapshot to reject invalid computer name: %q\n' "$invalid_name" >&2
      exit 1
    fi
    assert_contains "$tmp_home/invalid-name.err" "Snapshot directory resolution failed"
    assert_not_exists "$tmp_home/OneDrive/Backup/dotfiles/codex"
  done

  cleanup_home "$tmp_home"
}

test_snapshot_accepts_computer_name_containing_reserved_name_substring() {
  local tmp_home snapshot_dir

  tmp_home="$(new_home)"
  snapshot_dir="$tmp_home/OneDrive/Backup/dotfiles/codex/Mac mini construction/snapshots"
  mkdir -p "$tmp_home/.codex/sessions"

  HOME="$tmp_home" CODEX_BACKUP_COMPUTER_NAME="Mac mini construction" bash "$SNAPSHOT_SCRIPT"

  assert_file_exists "$tmp_home/Library/Logs/codex_snapshot_daily.log"
  if [ ! -d "$snapshot_dir" ]; then
    printf 'Expected normal computer name to resolve without rewriting: %s\n' "$snapshot_dir" >&2
    exit 1
  fi
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
  if grep -Fq "$staging_dir" "$state_dir/mv.log"; then
    printf 'Did not expect final publish rename to move directly from staging\n' >&2
    exit 1
  fi
  if find "$tmp_home/OneDrive/Backup/dotfiles/codex/$TEST_DEFAULT_COMPUTER_NAME/snapshots" -name '*.tmp.*' -print | grep -q .; then
    printf 'Did not expect snapshot tmp files under OneDrive snapshots\n' >&2
    exit 1
  fi
  if find "$staging_dir" -name '*.tmp.*' -print | grep -q .; then
    printf 'Did not expect snapshot tmp files left in staging\n' >&2
    exit 1
  fi

  cleanup_home "$tmp_home"
}

test_snapshot_allows_staging_dir_to_match_snapshot_dir() {
  local tmp_home archive_path snapshot_dir src_file

  tmp_home="$(new_home)"
  archive_path="$(snapshot_archive_path "$tmp_home")"
  snapshot_dir="$(dirname "$archive_path")"
  src_file="$tmp_home/.codex/sessions/day/rollout-staging-in-snapshot-dir.jsonl"

  mkdir -p "$(dirname "$src_file")"
  printf '{"step":1}\n' > "$src_file"

  HOME="$tmp_home" \
  CODEX_SNAPSHOT_STAGING_DIR="$snapshot_dir" \
  bash "$SNAPSHOT_SCRIPT"

  assert_file_exists "$archive_path"
  assert_archive_contains "$archive_path" "sessions/day/rollout-staging-in-snapshot-dir.jsonl"
  if find "$snapshot_dir" -name '*.tmp.*' -print | grep -q .; then
    printf 'Did not expect snapshot tmp files to remain when staging uses snapshot dir\n' >&2
    exit 1
  fi

  cleanup_home "$tmp_home"
}

test_snapshot_sanitizes_invalid_publish_rename_attempts() {
  local tmp_home archive_path err_path src_file

  tmp_home="$(new_home)"
  archive_path="$(snapshot_archive_path "$tmp_home")"
  err_path="$tmp_home/bad-attempts.err"
  src_file="$tmp_home/.codex/sessions/day/rollout-publish-bad-attempts.jsonl"

  mkdir -p "$(dirname "$src_file")"
  printf '{"step":1}\n' > "$src_file"

  HOME="$tmp_home" \
  CODEX_SNAPSHOT_PUBLISH_RENAME_ATTEMPTS=999999999999999999999999 \
  bash "$SNAPSHOT_SCRIPT" 2>"$err_path"

  assert_file_exists "$archive_path"
  assert_archive_contains "$archive_path" "sessions/day/rollout-publish-bad-attempts.jsonl"
  assert_not_contains "$err_path" "integer expression expected"

  cleanup_home "$tmp_home"
}

test_snapshot_sanitizes_invalid_publish_rename_delay() {
  local tmp_home archive_path log_path fake_bin mv_state sleep_state src_file staging_dir

  tmp_home="$(new_home)"
  archive_path="$(snapshot_archive_path "$tmp_home")"
  log_path="$tmp_home/Library/Logs/codex_snapshot_daily.log"
  fake_bin="$tmp_home/fake-bin"
  mv_state="$tmp_home/fake-mv-state"
  sleep_state="$tmp_home/fake-sleep-state"
  src_file="$tmp_home/.codex/sessions/day/rollout-publish-bad-delay.jsonl"
  staging_dir="$tmp_home/snapshot-staging"

  mkdir -p "$(dirname "$src_file")"
  setup_fake_mv_once "$fake_bin/mv"
  setup_fake_validating_sleep "$fake_bin/sleep"
  printf '{"step":1}\n' > "$src_file"

  HOME="$tmp_home" \
  PATH="$fake_bin:$PATH" \
  CODEX_SNAPSHOT_STAGING_DIR="$staging_dir" \
  CODEX_SNAPSHOT_PUBLISH_RENAME_ATTEMPTS=2 \
  CODEX_SNAPSHOT_PUBLISH_RENAME_DELAY_SECONDS=bad \
  TMP_FAKE_MV_STATE="$mv_state" \
  TMP_FAKE_MV_FAIL_DST="$archive_path" \
  TMP_FAKE_SLEEP_STATE="$sleep_state" \
  bash "$SNAPSHOT_SCRIPT"

  assert_file_exists "$archive_path"
  assert_archive_contains "$archive_path" "sessions/day/rollout-publish-bad-delay.jsonl"
  assert_contains "$log_path" "Snapshot publish rename failed (attempt 1/2)"
  assert_contains "$sleep_state/sleep.log" "sleep 5"

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
  assert_contains "$log_path" "Preserving staged snapshot for manual recovery: $staging_dir/"
  assert_not_contains "$log_path" "leaving temp file for manual recovery"
  if ! find "$staging_dir" -name '*.tmp.*' -print | grep -q .; then
    printf 'Expected snapshot tmp file to remain in staging after publish failure\n' >&2
    exit 1
  fi
  if find "$tmp_home/OneDrive/Backup/dotfiles/codex/$TEST_DEFAULT_COMPUTER_NAME/snapshots" -name '*.tmp.*' -print | grep -q .; then
    printf 'Did not expect snapshot publish tmp files to remain under OneDrive snapshots\n' >&2
    exit 1
  fi

  cleanup_home "$tmp_home"
}

test_snapshot_preserves_staging_file_when_publish_copy_fails() {
  local tmp_home archive_path log_path fake_bin state_dir src_file staging_dir snapshot_dir status
  local old_one old_two publish_marker gzip_marker remaining

  tmp_home="$(new_home)"
  archive_path="$(snapshot_archive_path "$tmp_home")"
  snapshot_dir="$(dirname "$archive_path")"
  log_path="$tmp_home/Library/Logs/codex_snapshot_daily.log"
  fake_bin="$tmp_home/fake-bin"
  state_dir="$tmp_home/fake-cp-state"
  src_file="$tmp_home/.codex/sessions/day/rollout-publish-copy-fails.jsonl"
  staging_dir="$tmp_home/snapshot-staging"

  mkdir -p "$(dirname "$src_file")"
  setup_fake_cp_publish_failure "$fake_bin/cp"
  printf '{"step":1}\n' > "$src_file"

  if command -v zstd >/dev/null 2>&1; then
    mkdir -p "$staging_dir"
    old_one="$staging_dir/codex-rollouts-2026-01-01.tar.zst.tmp.111"
    old_two="$staging_dir/codex-rollouts-2026-01-02.tar.zst.tmp.222"
    publish_marker="$staging_dir/codex-rollouts-2026-01-03.tar.zst.publish.tmp.333"
    gzip_marker="$staging_dir/codex-rollouts-2026-01-04.tar.gz.tmp.444"
    printf 'old one\n' > "$old_one"
    printf 'old two\n' > "$old_two"
    printf 'publish marker\n' > "$publish_marker"
    printf 'gzip marker\n' > "$gzip_marker"
  fi

  set +e
  HOME="$tmp_home" \
    PATH="$fake_bin:$PATH" \
    CODEX_SNAPSHOT_STAGING_DIR="$staging_dir" \
    TMP_FAKE_CP_STATE="$state_dir" \
    TMP_FAKE_CP_FAIL_DIR="$snapshot_dir" \
    bash "$SNAPSHOT_SCRIPT"
  status=$?
  set -e
  if [ "$status" -eq 0 ]; then
    printf 'Expected snapshot script to fail after publish copy failure\n' >&2
    exit 1
  fi
  if [ "$status" -ne 73 ]; then
    printf 'Expected publish copy failure exit status 73, got %s\n' "$status" >&2
    exit 1
  fi

  assert_not_exists "$archive_path"
  assert_contains "$log_path" "Snapshot publish copy failed"
  assert_contains "$state_dir/cp.log" "$snapshot_dir"
  if ! find "$staging_dir" -name '*.tmp.*' -print | grep -q .; then
    printf 'Expected snapshot tmp file to remain in staging after publish copy failure\n' >&2
    exit 1
  fi
  if find "$snapshot_dir" -name '*.tmp.*' -print | grep -q .; then
    printf 'Did not expect snapshot publish tmp files to remain under OneDrive snapshots\n' >&2
    exit 1
  fi
  if command -v zstd >/dev/null 2>&1; then
    assert_not_exists "$old_one"
    assert_not_exists "$old_two"
    assert_file_exists "$publish_marker"
    assert_file_exists "$gzip_marker"
    remaining="$(find "$staging_dir" -maxdepth 1 -type f -name 'codex-rollouts-*.tar.zst.tmp.*' -print | wc -l | tr -d ' ')"
    if [ "$remaining" -ne 1 ]; then
      printf 'Expected exactly one recoverable zstd staging tmp after publish failure, found %s\n' "$remaining" >&2
      exit 1
    fi
  fi

  cleanup_home "$tmp_home"
}

test_snapshot_rotates_only_zstd_staging_files() {
  local tmp_home src_file staging_dir old_one old_two publish_marker gzip_marker remaining

  if ! command -v zstd >/dev/null 2>&1; then
    return 0
  fi

  tmp_home="$(new_home)"
  src_file="$tmp_home/.codex/sessions/day/rollout-zstd-rotation.jsonl"
  staging_dir="$tmp_home/snapshot-staging"
  old_one="$staging_dir/codex-rollouts-2026-01-01.tar.zst.tmp.111"
  old_two="$staging_dir/codex-rollouts-2026-01-02.tar.zst.tmp.222"
  publish_marker="$staging_dir/codex-rollouts-2026-01-03.tar.zst.publish.tmp.333"
  gzip_marker="$staging_dir/codex-rollouts-2026-01-04.tar.gz.tmp.444"

  mkdir -p "$(dirname "$src_file")" "$staging_dir"
  printf '{"step":1}\n' > "$src_file"
  printf 'old one\n' > "$old_one"
  printf 'old two\n' > "$old_two"
  printf 'publish marker\n' > "$publish_marker"
  printf 'gzip marker\n' > "$gzip_marker"

  HOME="$tmp_home" \
    CODEX_SNAPSHOT_STAGING_DIR="$staging_dir" \
    bash "$SNAPSHOT_SCRIPT"

  assert_not_exists "$old_one"
  assert_not_exists "$old_two"
  assert_file_exists "$publish_marker"
  assert_file_exists "$gzip_marker"
  remaining="$(find "$staging_dir" -maxdepth 1 -type f -name 'codex-rollouts-*.tar.zst.tmp.*' -print | wc -l | tr -d ' ')"
  if [ "$remaining" -ne 0 ]; then
    printf 'Expected no zstd staging tmp after successful snapshot, found %s\n' "$remaining" >&2
    exit 1
  fi

  cleanup_home "$tmp_home"
}

test_snapshot_rejects_symlink_staging_before_touching_target() {
  local tmp_home src_file staging_dir staging_target old_tmp old_signature status err_path
  local target_tmp_count

  tmp_home="$(new_home)"
  src_file="$tmp_home/.codex/sessions/day/rollout-symlink-staging.jsonl"
  staging_dir="$tmp_home/snapshot-staging"
  staging_target="$tmp_home/staging-target"
  old_tmp="$staging_target/codex-rollouts-2026-01-01.tar.zst.tmp.111"
  err_path="$tmp_home/snapshot.err"

  mkdir -p "$(dirname "$src_file")" "$staging_target"
  printf '{"step":1}\n' > "$src_file"
  printf 'old staging data\n' > "$old_tmp"
  old_signature="$(/usr/bin/stat -f '%d:%i:%z:%Lp:%m' "$old_tmp")"
  ln -s "$staging_target" "$staging_dir"

  set +e
  HOME="$tmp_home" \
    CODEX_SNAPSHOT_STAGING_DIR="$staging_dir" \
    bash "$SNAPSHOT_SCRIPT" 2> "$err_path"
  status=$?
  set -e

  if [ "$status" -eq 0 ]; then
    printf 'Expected snapshot script to reject a symlink staging directory\n' >&2
    exit 1
  fi
  assert_contains "$err_path" "Snapshot staging path is not a non-symlink directory: $staging_dir"
  assert_file_exists "$old_tmp"
  if [ "$(/usr/bin/stat -f '%d:%i:%z:%Lp:%m' "$old_tmp")" != "$old_signature" ]; then
    printf 'Expected existing target zstd temp identity and metadata to remain unchanged\n' >&2
    exit 1
  fi
  assert_contains "$old_tmp" "old staging data"
  target_tmp_count="$(find "$staging_target" -maxdepth 1 -type f \
    -name '*.tmp.*' -print | wc -l | tr -d ' ')"
  if [ "$target_tmp_count" -ne 1 ]; then
    printf 'Expected symlink target to retain only the existing temp, found %s\n' \
      "$target_tmp_count" >&2
    exit 1
  fi

  cleanup_home "$tmp_home"
}

test_snapshot_rejects_source_root_symlink() {
  local tmp_home real_root codex_root status

  tmp_home="$(new_home)"
  real_root="$tmp_home/real-codex"
  codex_root="$tmp_home/codex-link"
  mkdir -p "$real_root/sessions"
  ln -s "$real_root" "$codex_root"

  set +e
  HOME="$tmp_home" CODEX_ROOT="$codex_root" bash "$SNAPSHOT_SCRIPT" \
    > "$tmp_home/source-root.out" 2> "$tmp_home/source-root.err"
  status=$?
  set -e

  if [ "$status" -ne 2 ]; then
    printf 'Expected source root symlink to be rejected with status 2, got %s\n' "$status" >&2
    exit 1
  fi
  assert_contains "$tmp_home/source-root.err" \
    "Snapshot source root is not a non-symlink directory: $codex_root"
  assert_not_exists "$(snapshot_archive_path "$tmp_home")"

  cleanup_home "$tmp_home"
}

test_snapshot_rejects_source_tree_symlink() {
  local tmp_home source_root sessions_link real_sessions status

  tmp_home="$(new_home)"
  source_root="$tmp_home/source-root"
  sessions_link="$source_root/sessions"
  real_sessions="$tmp_home/real-sessions"
  mkdir -p "$source_root" "$real_sessions"
  ln -s "$real_sessions" "$sessions_link"

  set +e
  HOME="$tmp_home" CODEX_ROOT="$source_root" bash "$SNAPSHOT_SCRIPT" \
    > "$tmp_home/source-tree.out" 2> "$tmp_home/source-tree.err"
  status=$?
  set -e

  if [ "$status" -ne 2 ]; then
    printf 'Expected source tree symlink to be rejected with status 2, got %s\n' "$status" >&2
    exit 1
  fi
  assert_contains "$tmp_home/source-tree.err" \
    "Snapshot source path is not a non-symlink directory: $sessions_link"
  assert_not_exists "$(snapshot_archive_path "$tmp_home")"

  cleanup_home "$tmp_home"
}

test_snapshot_rejects_output_nested_inside_source() {
  local tmp_home source_root snapshot_dir status

  tmp_home="$(new_home)"
  source_root="$tmp_home/source-root"
  snapshot_dir="$source_root/sessions/snapshot-output"
  mkdir -p "$source_root/sessions" "$source_root/archived_sessions"

  set +e
  HOME="$tmp_home" CODEX_ROOT="$source_root" CODEX_SNAPSHOT_DIR="$snapshot_dir" \
    bash "$SNAPSHOT_SCRIPT" > "$tmp_home/nested-output.out" 2> "$tmp_home/nested-output.err"
  status=$?
  set -e

  if [ "$status" -ne 2 ]; then
    printf 'Expected nested snapshot output to be rejected with status 2, got %s\n' "$status" >&2
    exit 1
  fi
  assert_contains "$tmp_home/nested-output.err" \
    "Snapshot output path is inside Codex source tree: $snapshot_dir"

  cleanup_home "$tmp_home"
}

test_snapshot_rejects_outputs_nested_under_missing_source_trees() {
  local tmp_home case_name source_root missing_tree unsafe_output status

  for case_name in missing-root staging-under-missing-sessions state-under-missing-archive; do
    tmp_home="$(new_home)"
    source_root="$tmp_home/source-root"
    case "$case_name" in
      missing-root)
        source_root="$tmp_home/missing-codex"
        missing_tree="$source_root/sessions"
        unsafe_output="$missing_tree/snapshot-output"
        ;;
      staging-under-missing-sessions)
        mkdir -p "$source_root/archived_sessions/old"
        missing_tree="$source_root/sessions"
        unsafe_output="$missing_tree/snapshot-staging"
        ;;
      state-under-missing-archive)
        mkdir -p "$source_root/sessions/existing"
        missing_tree="$source_root/archived_sessions"
        unsafe_output="$missing_tree/state"
        ;;
    esac

    set +e
    case "$case_name" in
      missing-root)
        HOME="$tmp_home" CODEX_ROOT="$source_root" CODEX_SNAPSHOT_DIR="$unsafe_output" \
          bash "$SNAPSHOT_SCRIPT" > "$tmp_home/missing-output.out" 2> "$tmp_home/missing-output.err"
        ;;
      staging-under-missing-sessions)
        HOME="$tmp_home" CODEX_ROOT="$source_root" CODEX_SNAPSHOT_DIR="$tmp_home/output" \
          CODEX_SNAPSHOT_STAGING_DIR="$unsafe_output" bash "$SNAPSHOT_SCRIPT" \
          > "$tmp_home/missing-output.out" 2> "$tmp_home/missing-output.err"
        ;;
      state-under-missing-archive)
        HOME="$tmp_home" CODEX_ROOT="$source_root" CODEX_SNAPSHOT_DIR="$tmp_home/output" \
          CODEX_BACKUP_STATE_ROOT="$unsafe_output" bash "$SNAPSHOT_SCRIPT" \
          > "$tmp_home/missing-output.out" 2> "$tmp_home/missing-output.err"
        ;;
    esac
    status=$?
    set -e

    if [ "$status" -ne 2 ]; then
      printf 'Expected nested output under missing source tree to fail with status 2 (%s), got %s\n' \
        "$case_name" "$status" >&2
      exit 1
    fi
    assert_contains "$tmp_home/missing-output.err" \
      "Snapshot output path is inside Codex source tree: $unsafe_output"
    assert_not_exists "$missing_tree"
    if [ "$case_name" = "missing-root" ]; then
      assert_not_exists "$source_root"
    fi

    cleanup_home "$tmp_home"
  done
}

test_snapshot_allows_output_symlink_alias_outside_source() {
  local tmp_home source_root output_target output_alias archive_path

  tmp_home="$(new_home)"
  source_root="$tmp_home/source-root"
  output_target="$tmp_home/onedrive-target"
  output_alias="$source_root/output-alias"
  mkdir -p "$source_root/sessions" "$source_root/archived_sessions" "$output_target"
  printf 'source bytes\n' > "$source_root/sessions/file"
  ln -s "$output_target" "$output_alias"

  HOME="$tmp_home" CODEX_ROOT="$source_root" CODEX_SNAPSHOT_DIR="$output_alias" \
    bash "$SNAPSHOT_SCRIPT"

  archive_path="$output_target/codex-rollouts-$(date +%Y-%m-%d).tar"
  if command -v zstd >/dev/null 2>&1; then
    archive_path="$archive_path.zst"
  else
    archive_path="$archive_path.gz"
  fi
  assert_file_exists "$archive_path"
  assert_archive_file_equals "$archive_path" "sessions/file" "$source_root/sessions/file"

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

if [ -n "${CODEX_LAUNCHD_TEST_SELECTOR:-}" ]; then
  case "$CODEX_LAUNCHD_TEST_SELECTOR" in
    test_*) ;;
    *)
      printf 'Invalid test selector: %s\n' "$CODEX_LAUNCHD_TEST_SELECTOR" >&2
      exit 2
      ;;
  esac
  if ! declare -F "$CODEX_LAUNCHD_TEST_SELECTOR" >/dev/null; then
    printf 'Unknown test selector: %s\n' "$CODEX_LAUNCHD_TEST_SELECTOR" >&2
    exit 2
  fi
  "$CODEX_LAUNCHD_TEST_SELECTOR"
  exit 0
fi

test_snapshot_skips_missing_source
test_snapshot_disables_bytecode_writes_for_python_children
test_snapshot_archives_complete_source_trees_without_touching_mirror
test_snapshot_archives_one_existing_source_tree
test_snapshot_skips_when_both_source_trees_are_missing
test_snapshot_does_not_publish_when_tar_fails
test_snapshot_does_not_publish_when_zstd_fails
test_snapshot_lock_rejects_unsafe_lock_leaves
test_snapshot_lock_detects_path_replacement
test_snapshot_lock_detects_state_ancestor_replacement
test_snapshot_lock_rejects_concurrent_zstd_run
test_snapshot_rejects_source_root_symlink
test_snapshot_rejects_source_tree_symlink
test_snapshot_rejects_output_nested_inside_source
test_snapshot_rejects_outputs_nested_under_missing_source_trees
test_snapshot_allows_output_symlink_alias_outside_source
test_sync_safe_helper_uses_apfs_reflink_and_absent_private_stage
test_sync_safe_helper_rejects_inherited_destination_acl
test_sync_requires_nonempty_log_before_touching_mirror_state
test_snapshot_waits_for_onedrive_readiness_before_unpin
test_snapshot_archives_empty_source
test_snapshot_can_rerun_same_day
test_snapshot_default_paths_follow_runtime_computer_name
test_snapshot_explicit_directory_does_not_require_computer_name
test_snapshot_fails_closed_without_valid_computer_name
test_snapshot_accepts_computer_name_containing_reserved_name_substring
test_snapshot_retries_transient_publish_rename_failure
test_snapshot_allows_staging_dir_to_match_snapshot_dir
test_snapshot_sanitizes_invalid_publish_rename_attempts
test_snapshot_sanitizes_invalid_publish_rename_delay
test_snapshot_preserves_staging_file_when_publish_retries_are_exhausted
test_snapshot_preserves_staging_file_when_publish_copy_fails
test_snapshot_rotates_only_zstd_staging_files
test_snapshot_rejects_symlink_staging_before_touching_target
test_sync_tolerates_missing_current_mirror_during_stat
test_sync_tolerates_missing_duplicate_mirror_during_stat
test_install_generates_portable_launchd_plist
