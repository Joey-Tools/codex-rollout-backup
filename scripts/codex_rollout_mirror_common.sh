#!/bin/bash

CODEX_ROOT="${CODEX_ROOT:-$HOME/.codex}"
CODEX_BACKUP_BASE="${CODEX_BACKUP_BASE:-$HOME/.dotfiles/codex-backup}"
CODEX_MIRROR_ROOT="${CODEX_MIRROR_ROOT:-$CODEX_BACKUP_BASE/mirror}"
CODEX_BACKUP_STATE_ROOT="${CODEX_BACKUP_STATE_ROOT:-$CODEX_BACKUP_BASE/state}"
CODEX_ROLLOUT_MIRROR_COPY_HELPER="${CODEX_ROLLOUT_MIRROR_COPY_HELPER:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/codex_rollout_mirror_copy.py}"
CODEX_ROLLOUT_MIRROR_COPY_PYTHON="${CODEX_ROLLOUT_MIRROR_COPY_PYTHON:-python3}"

file_size() {
  /usr/bin/stat -f%z "$1"
}

have_codex_rollout_source_dirs() {
  [ -d "$CODEX_ROOT/sessions" ] || [ -d "$CODEX_ROOT/archived_sessions" ]
}

cleanup_codex_rollout_path_inventory() {
  local inventory_dir="$1"
  local cleanup_status=0

  [ -n "$inventory_dir" ] || return 0
  if [ -e "$inventory_dir/paths" ] || [ -L "$inventory_dir/paths" ]; then
    rm -f -- "$inventory_dir/paths" || cleanup_status=1
  fi
  if [ -d "$inventory_dir" ]; then
    rmdir "$inventory_dir" || cleanup_status=1
  fi
  return "$cleanup_status"
}

rollout_rel_is_index_safe() {
  local rel="$1"

  case "$rel" in
    sessions/* | archived_sessions/*)
      ;;
    *)
      return 1
      ;;
  esac
  case "$rel" in
    *$'\t'* | *$'\n'* | *$'\\'*)
      return 1
      ;;
  esac
  return 0
}

rollout_root_is_index_safe() {
  local root="$1"

  [ -n "$root" ] || return 1
  case "$root" in
    *$'\t'* | *$'\n'* | *$'\\'*)
      return 1
      ;;
  esac
  return 0
}

rollout_root_has_safe_lexical_form() {
  local root="$1"

  [ "$root" = "/" ] && return 1
  case "$root" in
    */ | *//* | */./* | */../* | */. | */..)
      return 1
      ;;
  esac
  return 0
}

validate_codex_rollout_index_configuration() {
  local root

  case "$CODEX_ROOT" in
    /*)
      ;;
    *)
      printf 'CODEX_ROOT must be absolute.\n' >&2
      return 2
      ;;
  esac
  case "$CODEX_MIRROR_ROOT" in
    /*)
      ;;
    *)
      printf 'CODEX_MIRROR_ROOT must be absolute.\n' >&2
      return 2
      ;;
  esac
  if ! rollout_root_is_index_safe "$CODEX_ROOT"; then
    printf 'CODEX_ROOT contains an unsupported tab, newline, or backslash.\n' >&2
    return 2
  fi
  if ! rollout_root_is_index_safe "$CODEX_MIRROR_ROOT"; then
    printf 'CODEX_MIRROR_ROOT contains an unsupported tab, newline, or backslash.\n' >&2
    return 2
  fi
  if ! rollout_root_has_safe_lexical_form "$CODEX_ROOT"; then
    printf 'CODEX_ROOT is the filesystem root or has a trailing slash, empty component, or dot component.\n' >&2
    return 2
  fi
  if ! rollout_root_has_safe_lexical_form "$CODEX_MIRROR_ROOT"; then
    printf 'CODEX_MIRROR_ROOT is the filesystem root or has a trailing slash, empty component, or dot component.\n' >&2
    return 2
  fi
  if [ -e "$CODEX_ROOT" ] || [ -L "$CODEX_ROOT" ]; then
    if [ ! -d "$CODEX_ROOT" ] || [ -L "$CODEX_ROOT" ]; then
      printf 'The configured CODEX_ROOT is not a non-symlink directory.\n' >&2
      return 2
    fi
  fi
  for root in "$CODEX_ROOT/sessions" "$CODEX_ROOT/archived_sessions"; do
    if [ -e "$root" ] || [ -L "$root" ]; then
      if [ ! -d "$root" ] || [ -L "$root" ]; then
        printf 'A configured rollout source root is not a non-symlink directory.\n' >&2
        return 2
      fi
    fi
  done
  if [ -e "$CODEX_MIRROR_ROOT" ] || [ -L "$CODEX_MIRROR_ROOT" ]; then
    if [ ! -d "$CODEX_MIRROR_ROOT" ] || [ -L "$CODEX_MIRROR_ROOT" ]; then
      printf 'The configured rollout mirror root is not a non-symlink directory.\n' >&2
      return 2
    fi
  fi
}

validate_codex_rollout_path_inventory() {
  local root="$1"
  local path_index="$2"
  local inventory_name="$3"
  local path rel

  while IFS= read -r -d '' path; do
    case "$path" in
      "$root"/*)
        rel="${path#"$root"/}"
        ;;
      *)
        printf 'Rollout %s escaped its configured root.\n' "$inventory_name" >&2
        return 2
        ;;
    esac
    if ! rollout_rel_is_index_safe "$rel"; then
      printf 'Unsupported rollout %s path: %q\n' "$inventory_name" "$rel" >&2
      return 2
    fi
  done < "$path_index"
}

find_codex_rollout_sources() (
  local inventory_dir=""
  local path_index
  local root
  local inventory_exit

  validate_codex_rollout_index_configuration || return $?
  if ! inventory_dir="$(mktemp -d "${TMPDIR:-/tmp}/codex-rollout-paths.XXXXXX")"; then
    return 1
  fi
  path_index="$inventory_dir/paths"
  trap '
    inventory_exit=$?
    trap - EXIT
    if ! cleanup_codex_rollout_path_inventory "$inventory_dir"; then
      [ "$inventory_exit" -ne 0 ] || inventory_exit=1
    fi
    exit "$inventory_exit"
  ' EXIT
  : > "$path_index" || return 1

  for root in "$CODEX_ROOT/sessions" "$CODEX_ROOT/archived_sessions"; do
    if [ ! -e "$root" ] && [ ! -L "$root" ]; then
      continue
    fi
    if [ ! -d "$root" ] || [ -L "$root" ]; then
      printf 'A rollout source root changed type before enumeration.\n' >&2
      return 2
    fi
    find "$root" -type f -name "rollout-*.jsonl" -size +0 -print0 >> "$path_index" || return 1
  done
  validate_codex_rollout_path_inventory "$CODEX_ROOT" "$path_index" "source" || return $?
  /bin/cat "$path_index" || return 1
)

find_codex_rollout_mirror_files() (
  local inventory_dir=""
  local path_index
  local inventory_exit

  validate_codex_rollout_index_configuration || return $?
  [ -d "$CODEX_MIRROR_ROOT" ] || return 0
  if [ -L "$CODEX_MIRROR_ROOT" ]; then
    printf 'The rollout mirror root changed to a symlink before enumeration.\n' >&2
    return 2
  fi
  if ! inventory_dir="$(mktemp -d "${TMPDIR:-/tmp}/codex-rollout-paths.XXXXXX")"; then
    return 1
  fi
  path_index="$inventory_dir/paths"
  trap '
    inventory_exit=$?
    trap - EXIT
    if ! cleanup_codex_rollout_path_inventory "$inventory_dir"; then
      [ "$inventory_exit" -ne 0 ] || inventory_exit=1
    fi
    exit "$inventory_exit"
  ' EXIT
  : > "$path_index" || return 1

  find "$CODEX_MIRROR_ROOT" -type f -name "rollout-*.jsonl" -size +0 -print0 > "$path_index" || return 1
  validate_codex_rollout_path_inventory "$CODEX_MIRROR_ROOT" "$path_index" "mirror" || return $?
  /bin/cat "$path_index" || return 1
)

run_codex_rollout_mirror_test_hook() {
  local stage="$1"
  local path="$2"
  local rel="$3"
  local hook="${CODEX_ROLLOUT_MIRROR_TEST_HOOK:-}"

  [ -n "$hook" ] || return 0
  "$hook" "$stage" "$path" "$rel"
}

run_codex_rollout_mirror_sync_one() {
  local source="$1"
  local destination="$2"

  "$CODEX_ROLLOUT_MIRROR_COPY_PYTHON" \
    "$CODEX_ROLLOUT_MIRROR_COPY_HELPER" \
    sync-one \
    --source "$source" \
    --destination "$destination" \
    --json
}

validate_codex_rollout_mirror_sync_receipt() {
  local source="$1"
  local destination="$2"
  local exit_status="$3"

  "$CODEX_ROLLOUT_MIRROR_COPY_PYTHON" \
    "$CODEX_ROLLOUT_MIRROR_COPY_HELPER" \
    validate-receipt \
    --source "$source" \
    --destination "$destination" \
    --exit-status "$exit_status"
}

build_mirror_index() {
  local output="$1"
  local path_index="$2"
  local file rel base

  : > "$output"

  while IFS= read -r -d '' file; do
    rel="${file#"$CODEX_MIRROR_ROOT"/}"
    if ! rollout_rel_is_index_safe "$rel"; then
      printf 'Unsupported mirror rollout path after validation: %q\n' "$rel" >> "$LOG"
      return 2
    fi
    base="$(basename "$file")"
    printf '%s\t%s\t%s\n' "$base" "$rel" "$file" >> "$output"
  done < "$path_index"
}

build_stale_mirror_match_index() {
  local output="$1"
  local mirror_index="$2"
  local active_rel_index="$3"
  local base="$4"
  local rel="$5"

  awk -F '\t' -v wanted="$base" -v current="$rel" '
    NR == FNR {
      active[$1] = 1
      next
    }

    $1 == wanted && $2 != current && !($2 in active) {
      print $2 "\t" $3
    }
  ' "$active_rel_index" "$mirror_index" > "$output"
}

file_size_if_present() {
  local path="$1"
  local size

  if size=$(file_size "$path" 2>/dev/null); then
    printf '%s\n' "$size"
    return 0
  fi

  [ -e "$path" ] || return 1
  return 2
}

record_touched_mirror_file() {
  local touched_list="$1"
  local path="$2"

  [ -n "$touched_list" ] || return 0
  printf '%s\0' "$path" >> "$touched_list"
}

record_mirror_relocation() {
  local relocation_list="$1"
  local from_rel="$2"
  local to_rel="$3"

  [ -n "$relocation_list" ] || return 0
  printf '%s\t%s\n' "$from_rel" "$to_rel" >> "$relocation_list"
}

record_suppressed_mirror_rel() {
  local suppressed_list="$1"
  local rel="$2"

  [ -n "$suppressed_list" ] || return 0
  printf '%s\0' "$rel" >> "$suppressed_list"
}

mirror_rel_is_suppressed() {
  local suppressed_list="$1"
  local rel="$2"
  local suppressed_rel

  [ -n "$suppressed_list" ] || return 1
  [ -f "$suppressed_list" ] || return 1
  while IFS= read -r -d '' suppressed_rel; do
    [ "$suppressed_rel" = "$rel" ] && return 0
  done < "$suppressed_list"
  return 1
}

suppress_unstable_rollout_family() {
  local suppressed_list="$1"
  local rel="$2"
  local relocated_from="$3"
  local duplicate_index="$4"
  local duplicate_rel duplicate_path

  record_suppressed_mirror_rel "$suppressed_list" "$rel"
  if [ -n "$relocated_from" ]; then
    record_suppressed_mirror_rel "$suppressed_list" "$relocated_from"
  fi
  while IFS=$'\t' read -r duplicate_rel duplicate_path; do
    record_suppressed_mirror_rel "$suppressed_list" "$duplicate_rel"
  done < "$duplicate_index"
}

remove_stale_mirror_file() {
  local stale_rel="$1"
  local stale_path="$2"
  local current_rel="$3"
  local relocation_list="$4"
  local action="$5"

  if [ -e "$stale_path" ] || [ -L "$stale_path" ]; then
    if [ ! -f "$stale_path" ] || [ -L "$stale_path" ]; then
      return 1
    fi
    rm -f -- "$stale_path" || return 1
    if [ "$action" = "relocate" ]; then
      echo "Mirror relocate $stale_rel -> $current_rel" >> "$LOG"
    else
      echo "Mirror prune stale duplicate $stale_rel -> $current_rel" >> "$LOG"
    fi
  fi
  record_mirror_relocation "$relocation_list" "$stale_rel" "$current_rel"
}

remove_matching_stale_mirror_file() {
  local stale_rel="$1"
  local stale_path="$2"
  local current_rel="$3"
  local current_path="$4"
  local current_size="$5"
  local relocation_list="$6"
  local action="$7"
  local stale_size stat_status

  if [ ! -e "$stale_path" ] && [ ! -L "$stale_path" ]; then
    return 0
  fi
  if [ ! -f "$stale_path" ] || [ -L "$stale_path" ]; then
    return 1
  fi
  if stale_size=$(file_size_if_present "$stale_path"); then
    :
  else
    stat_status=$?
    [ "$stat_status" -eq 1 ] && return 0
    return 1
  fi
  if [ "$stale_size" -ne "$current_size" ] || ! cmp -s -- "$stale_path" "$current_path"; then
    return 0
  fi
  remove_stale_mirror_file \
    "$stale_rel" \
    "$stale_path" \
    "$current_rel" \
    "$relocation_list" \
    "$action"
}

cleanup_codex_rollout_sync_indexes() {
  local sync_tmp_dir="$1"
  local cleanup_status=0
  local path

  [ -n "$sync_tmp_dir" ] || return 0
  for path in \
    "$sync_tmp_dir/source-paths" \
    "$sync_tmp_dir/sources" \
    "$sync_tmp_dir/source-rels" \
    "$sync_tmp_dir/mirror-paths" \
    "$sync_tmp_dir/mirrors" \
    "$sync_tmp_dir/stale" \
    "$sync_tmp_dir/duplicates"; do
    if [ -e "$path" ] || [ -L "$path" ]; then
      rm -f -- "$path" || cleanup_status=1
    fi
  done
  if [ -d "$sync_tmp_dir" ]; then
    rmdir "$sync_tmp_dir" || cleanup_status=1
  fi
  return "$cleanup_status"
}

sync_codex_rollout_mirror() (
  local touched_list="${1:-}"
  local relocation_list="${2:-}"
  local suppressed_list="${3:-}"
  local sync_tmp_dir=""
  local source_path_index
  local source_index
  local source_rel_index
  local mirror_path_index
  local mirror_index
  local stale_match_index
  local duplicate_index
  local src
  local rel
  local base
  local dst
  local other_rel
  local other_path
  local relocated
  local relocated_from
  local relocated_path
  local duplicate_rel
  local duplicate_path
  local src_size
  local dst_size
  local stat_status
  local refresh_needed
  local sync_receipt
  local sync_receipt_fields
  local sync_status
  local sync_outcome
  local sync_old_size
  local sync_new_size
  local sync_method
  local log_old_size
  local sync_exit
  local inventory_status
  local status=0

  if [ -z "${LOG:-}" ]; then
    printf 'LOG must be set before syncing the Codex rollout mirror.\n' >&2
    return 2
  fi
  validate_codex_rollout_index_configuration || return $?

  if ! sync_tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/codex-rollout-sync.XXXXXX")"; then
    return 1
  fi
  trap '
    sync_exit=$?
    trap - EXIT
    if ! cleanup_codex_rollout_sync_indexes "$sync_tmp_dir"; then
      [ "$sync_exit" -ne 0 ] || sync_exit=1
    fi
    exit "$sync_exit"
  ' EXIT
  set -e

  mkdir -p "$CODEX_MIRROR_ROOT" "$CODEX_BACKUP_STATE_ROOT"
  source_path_index="$sync_tmp_dir/source-paths"
  source_index="$sync_tmp_dir/sources"
  source_rel_index="$sync_tmp_dir/source-rels"
  mirror_path_index="$sync_tmp_dir/mirror-paths"
  mirror_index="$sync_tmp_dir/mirrors"
  stale_match_index="$sync_tmp_dir/stale"
  duplicate_index="$sync_tmp_dir/duplicates"
  : > "$source_index"
  : > "$source_rel_index"
  : > "$mirror_index"
  : > "$stale_match_index"
  : > "$duplicate_index"

  if [ -n "$touched_list" ]; then
    : > "$touched_list"
  fi
  if [ -n "$relocation_list" ]; then
    : > "$relocation_list"
  fi
  if [ -n "$suppressed_list" ]; then
    : > "$suppressed_list"
  fi

  if find_codex_rollout_sources > "$source_path_index"; then
    :
  else
    inventory_status=$?
    echo "Failed to enumerate Codex rollout sources." >> "$LOG"
    return "$inventory_status"
  fi
  while IFS= read -r -d '' src; do
    rel="${src#"$CODEX_ROOT"/}"
    if ! rollout_rel_is_index_safe "$rel"; then
      printf 'Unsupported source rollout path after validation: %q\n' "$rel" >> "$LOG"
      return 2
    fi
    base="$(basename "$src")"
    printf '%s\t%s\t%s\n' "$base" "$rel" "$src" >> "$source_index"
    printf '%s\n' "$rel" >> "$source_rel_index"
  done < "$source_path_index"

  if find_codex_rollout_mirror_files > "$mirror_path_index"; then
    :
  else
    inventory_status=$?
    echo "Failed to enumerate Codex rollout mirror files." >> "$LOG"
    return "$inventory_status"
  fi
  build_mirror_index "$mirror_index" "$mirror_path_index"

  while IFS=$'\t' read -r base rel src; do
    dst="$CODEX_MIRROR_ROOT/$rel"
    relocated=0
    relocated_from=""
    relocated_path=""
    : > "$duplicate_index"

    if ! build_stale_mirror_match_index "$stale_match_index" "$mirror_index" "$source_rel_index" "$base" "$rel"; then
      status=1
      break
    fi

    # Keep stale paths in place until the held-FD helper reaches a safe success.
    while IFS=$'\t' read -r other_rel other_path; do
      [ -f "$other_path" ] || continue
      if [ ! -e "$dst" ] && [ ! -L "$dst" ] && [ "$relocated" -eq 0 ]; then
        relocated=1
        relocated_from="$other_rel"
        relocated_path="$other_path"
        continue
      fi

      printf '%s\t%s\n' "$other_rel" "$other_path" >> "$duplicate_index"
    done < "$stale_match_index"

    run_codex_rollout_mirror_test_hook "before_stat" "$src" "$rel"
    if ! src_size=$(file_size "$src" 2>/dev/null); then
      if [ ! -e "$src" ]; then
        echo "Source rollout disappeared during mirror sync, skipping: $rel" >> "$LOG"
        suppress_unstable_rollout_family \
          "$suppressed_list" \
          "$rel" \
          "$relocated_from" \
          "$duplicate_index"
        continue
      fi
      status=1
      break
    fi
    dst_size=0
    if [ -f "$dst" ]; then
      if dst_size=$(file_size_if_present "$dst"); then
        :
      else
        stat_status=$?
        case "$stat_status" in
          1)
            dst_size=0
            ;;
          *)
            echo "Failed to stat mirror rollout during sync: $rel" >> "$LOG"
            status=1
            break
            ;;
        esac
      fi
    fi

    # Size is only an append-only performance hint. Every mutating refresh is
    # re-opened and revalidated by the helper before it copies or publishes.
    refresh_needed=0
    if [ "$relocated" -eq 1 ] || [ -s "$duplicate_index" ] || [ "$src_size" -ne "$dst_size" ]; then
      refresh_needed=1
    fi

    if [ "$refresh_needed" -eq 0 ]; then
      continue
    fi

    mkdir -p "$(dirname "$dst")"
    run_codex_rollout_mirror_test_hook "before_copy" "$src" "$rel"
    sync_receipt=""
    if sync_receipt=$(run_codex_rollout_mirror_sync_one "$src" "$dst" 2>> "$LOG"); then
      sync_status=0
    else
      sync_status=$?
    fi
    if ! sync_receipt_fields=$(printf '%s\n' "$sync_receipt" | \
      validate_codex_rollout_mirror_sync_receipt "$src" "$dst" "$sync_status" 2>> "$LOG"); then
      echo "Invalid safe mirror sync receipt: $rel (status $sync_status)" >> "$LOG"
      status=2
      break
    fi
    IFS=$'\t' read -r sync_outcome sync_old_size sync_new_size sync_method <<< "$sync_receipt_fields"

    case "$sync_outcome" in
      deferred)
        echo "Source rollout disappeared during mirror sync, skipping: $rel" >> "$LOG"
        suppress_unstable_rollout_family \
          "$suppressed_list" \
          "$rel" \
          "$relocated_from" \
          "$duplicate_index"
        continue
        ;;
      fatal)
        echo "Safe mirror sync failed: $rel (status $sync_status)" >> "$LOG"
        status=2
        break
        ;;
      no-complete-line)
        echo "Mirror source has no complete line, retaining current mirror: $rel (method=$sync_method)" >> "$LOG"
        if [ "$sync_new_size" = "null" ]; then
          suppress_unstable_rollout_family \
            "$suppressed_list" \
            "$rel" \
            "$relocated_from" \
            "$duplicate_index"
          continue
        fi
        if [ "$relocated" -eq 1 ] && ! remove_matching_stale_mirror_file \
          "$relocated_from" \
          "$relocated_path" \
          "$rel" \
          "$dst" \
          "$sync_new_size" \
          "$relocation_list" \
          "relocate"; then
          echo "Failed to compare delayed mirror relocation during sync: $relocated_from" >> "$LOG"
          status=1
          break
        fi
        while IFS=$'\t' read -r duplicate_rel duplicate_path; do
          if ! remove_matching_stale_mirror_file \
            "$duplicate_rel" \
            "$duplicate_path" \
            "$rel" \
            "$dst" \
            "$sync_new_size" \
            "$relocation_list" \
            "duplicate"; then
            echo "Failed to compare duplicate mirror rollout during sync: $duplicate_rel" >> "$LOG"
            status=1
            break
          fi
        done < "$duplicate_index"
        [ "$status" -eq 0 ] || break
        continue
        ;;
      updated | unchanged)
        ;;
      *)
        echo "Invalid safe mirror sync outcome after validation: $rel" >> "$LOG"
        status=2
        break
        ;;
    esac

    log_old_size="$sync_old_size"
    if [ "$log_old_size" = "null" ]; then
      log_old_size=0
    fi
    if [ "$sync_outcome" = "updated" ]; then
      echo "Mirror update $rel ($log_old_size -> $sync_new_size, method=$sync_method)" >> "$LOG"
      record_touched_mirror_file "$touched_list" "$dst"
    else
      echo "Mirror refresh unchanged $rel ($sync_new_size, method=$sync_method)" >> "$LOG"
    fi

    if [ "$relocated" -eq 1 ] && ! remove_stale_mirror_file \
      "$relocated_from" \
      "$relocated_path" \
      "$rel" \
      "$relocation_list" \
      "relocate"; then
      echo "Failed to commit delayed mirror relocation: $relocated_from -> $rel" >> "$LOG"
      status=1
      break
    fi
    while IFS=$'\t' read -r duplicate_rel duplicate_path; do
      if ! remove_stale_mirror_file \
        "$duplicate_rel" \
        "$duplicate_path" \
        "$rel" \
        "$relocation_list" \
        "duplicate"; then
        echo "Failed to prune stale duplicate during sync: $duplicate_rel" >> "$LOG"
        status=1
        break
      fi
    done < "$duplicate_index"
    [ "$status" -eq 0 ] || break
  done < "$source_index"

  return "$status"
)
