#!/bin/bash

CODEX_ROOT="${CODEX_ROOT:-$HOME/.codex}"
CODEX_BACKUP_BASE="${CODEX_BACKUP_BASE:-$HOME/.dotfiles/codex-backup}"
CODEX_MIRROR_ROOT="${CODEX_MIRROR_ROOT:-$CODEX_BACKUP_BASE/mirror}"
CODEX_BACKUP_STATE_ROOT="${CODEX_BACKUP_STATE_ROOT:-$CODEX_BACKUP_BASE/state}"

file_size() {
  /usr/bin/stat -f%z "$1"
}

have_codex_rollout_source_dirs() {
  [ -d "$CODEX_ROOT/sessions" ] || [ -d "$CODEX_ROOT/archived_sessions" ]
}

find_codex_rollout_sources() {
  local root

  for root in "$CODEX_ROOT/sessions" "$CODEX_ROOT/archived_sessions"; do
    [ -d "$root" ] || continue
    find "$root" -type f -name "rollout-*.jsonl" -size +0 -print0
  done
}

find_codex_rollout_mirror_files() {
  [ -d "$CODEX_MIRROR_ROOT" ] || return 0
  find "$CODEX_MIRROR_ROOT" -type f -name "rollout-*.jsonl" -size +0 -print0
}

run_codex_rollout_mirror_test_hook() {
  local stage="$1"
  local path="$2"
  local rel="$3"
  local hook="${CODEX_ROLLOUT_MIRROR_TEST_HOOK:-}"

  [ -n "$hook" ] || return 0
  "$hook" "$stage" "$path" "$rel"
}

gnu_cp_for_reflink() {
  local candidate

  for candidate in "$(command -v cp 2>/dev/null || true)" "$(command -v gcp 2>/dev/null || true)"; do
    [ -n "$candidate" ] || continue
    if "$candidate" --help 2>&1 | grep -Fq -- '--reflink'; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done

  return 1
}

copy_with_reflink_preference() {
  local src="$1"
  local dst="$2"
  local gnu_cp

  if gnu_cp=$(gnu_cp_for_reflink); then
    if "$gnu_cp" --reflink=always -- "$src" "$dst" 2>/dev/null; then
      return 0
    fi
  fi

  cp -p "$src" "$dst"
}

last_complete_line_size() {
  /usr/bin/perl -e '
    use strict;
    use warnings;
    use bytes;

    my $path = shift @ARGV;
    open my $fh, "<", $path or die "$path: $!";
    binmode $fh;

    my $offset = 0;
    my $last_complete = 0;

    while (read($fh, my $buf, 65536)) {
        my $cursor = 0;
        while (1) {
            my $idx = index($buf, "\n", $cursor);
            last if $idx < 0;
            $last_complete = $offset + $idx + 1;
            $cursor = $idx + 1;
        }
        $offset += length($buf);
    }

    print "$last_complete\n";
  ' "$1"
}

truncate_file_to_size() {
  /usr/bin/perl -e '
    use strict;
    use warnings;

    my ($path, $size) = @ARGV;
    truncate($path, $size) or die "$path: $!";
  ' "$1" "$2"
}

build_mirror_index() {
  local output="$1"
  local file rel base

  : > "$output"

  while IFS= read -r -d '' file; do
    rel="${file#"$CODEX_MIRROR_ROOT"/}"
    base="$(basename "$file")"
    printf '%s\t%s\t%s\n' "$base" "$rel" "$file" >> "$output"
  done < <(find_codex_rollout_mirror_files)
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
  printf '%s\n' "$rel" >> "$suppressed_list"
}

mirror_rel_is_suppressed() {
  local suppressed_list="$1"
  local rel="$2"

  [ -n "$suppressed_list" ] || return 1
  [ -f "$suppressed_list" ] || return 1
  grep -Fxq -- "$rel" "$suppressed_list"
}

rollback_mirror_relocation_if_needed() {
  local relocated="$1"
  local from_rel="$2"
  local to_path="$3"

  [ "$relocated" -eq 1 ] || return 0
  [ -n "$from_rel" ] || return 0
  [ -f "$to_path" ] || return 0

  local rollback_path="$CODEX_MIRROR_ROOT/$from_rel"
  if [ -e "$rollback_path" ]; then
    return 0
  fi

  mkdir -p "$(dirname "$rollback_path")"
  mv "$to_path" "$rollback_path"
  echo "Mirror relocate rollback ${to_path#"$CODEX_MIRROR_ROOT"/} -> $from_rel" >> "$LOG"
}

record_committed_mirror_relocation() {
  local relocated="$1"
  local relocation_list="$2"
  local from_rel="$3"
  local to_rel="$4"

  [ "$relocated" -eq 1 ] || return 0
  record_mirror_relocation "$relocation_list" "$from_rel" "$to_rel"
}

sync_codex_rollout_mirror() {
  local touched_list="${1:-}"
  local relocation_list="${2:-}"
  local suppressed_list="${3:-}"
  local source_index
  local source_rel_index
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
  local duplicate_rel
  local duplicate_path
  local duplicate_size
  local src_size
  local dst_size
  local stat_status
  local tmp
  local complete_size
  local refresh_needed
  local status=0

  mkdir -p "$CODEX_MIRROR_ROOT" "$CODEX_BACKUP_STATE_ROOT"
  source_index="$(mktemp "${TMPDIR:-/tmp}/codex-rollout-source.XXXXXX")"
  source_rel_index="$(mktemp "${TMPDIR:-/tmp}/codex-rollout-source-rel.XXXXXX")"
  mirror_index="$(mktemp "${TMPDIR:-/tmp}/codex-rollout-mirror.XXXXXX")"
  stale_match_index="$(mktemp "${TMPDIR:-/tmp}/codex-rollout-stale.XXXXXX")"
  duplicate_index="$(mktemp "${TMPDIR:-/tmp}/codex-rollout-duplicate.XXXXXX")"

  if [ -n "$touched_list" ]; then
    : > "$touched_list"
  fi
  if [ -n "$relocation_list" ]; then
    : > "$relocation_list"
  fi
  if [ -n "$suppressed_list" ]; then
    : > "$suppressed_list"
  fi

  while IFS= read -r -d '' src; do
    rel="${src#"$CODEX_ROOT"/}"
    base="$(basename "$src")"
    printf '%s\t%s\t%s\n' "$base" "$rel" "$src" >> "$source_index"
    printf '%s\n' "$rel" >> "$source_rel_index"
  done < <(find_codex_rollout_sources)

  build_mirror_index "$mirror_index"

  while IFS=$'\t' read -r base rel src; do
    dst="$CODEX_MIRROR_ROOT/$rel"
    relocated=0
    relocated_from=""
    : > "$duplicate_index"

    if ! build_stale_mirror_match_index "$stale_match_index" "$mirror_index" "$source_rel_index" "$base" "$rel"; then
      status=1
      break
    fi

    while IFS=$'\t' read -r other_rel other_path; do
      [ -f "$other_path" ] || continue
      if [ ! -e "$dst" ] && [ "$relocated" -eq 0 ]; then
        mkdir -p "$(dirname "$dst")"
        mv "$other_path" "$dst"
        relocated=1
        relocated_from="$other_rel"
        echo "Mirror relocate $other_rel -> $rel" >> "$LOG"
        continue
      fi

      printf '%s\t%s\n' "$other_rel" "$other_path" >> "$duplicate_index"
    done < "$stale_match_index"

    run_codex_rollout_mirror_test_hook "before_stat" "$src" "$rel"
    if ! src_size=$(file_size "$src" 2>/dev/null); then
      if [ ! -e "$src" ]; then
        rollback_mirror_relocation_if_needed "$relocated" "$relocated_from" "$dst"
        echo "Source rollout disappeared during mirror sync, skipping: $rel" >> "$LOG"
        record_suppressed_mirror_rel "$suppressed_list" "$rel"
        record_suppressed_mirror_rel "$suppressed_list" "$relocated_from"
        while IFS=$'\t' read -r duplicate_rel duplicate_path; do
          record_suppressed_mirror_rel "$suppressed_list" "$duplicate_rel"
        done < "$duplicate_index"
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

    refresh_needed=0
    if [ -s "$duplicate_index" ] || [ "$src_size" -ne "$dst_size" ]; then
      refresh_needed=1
    fi

    if [ "$refresh_needed" -eq 0 ]; then
      if [ "$relocated" -eq 1 ]; then
        record_committed_mirror_relocation "$relocated" "$relocation_list" "$relocated_from" "$rel"
        record_touched_mirror_file "$touched_list" "$dst"
      fi
      while IFS=$'\t' read -r duplicate_rel duplicate_path; do
        if [ -f "$duplicate_path" ]; then
          rm -f "$duplicate_path"
          echo "Mirror prune stale duplicate $duplicate_rel -> $rel" >> "$LOG"
        fi
        record_mirror_relocation "$relocation_list" "$duplicate_rel" "$rel"
      done < "$duplicate_index"
      continue
    fi

    mkdir -p "$(dirname "$dst")"
    tmp="$(mktemp "${dst}.tmp.XXXXXX")"
    run_codex_rollout_mirror_test_hook "before_copy" "$src" "$rel"
    if ! copy_with_reflink_preference "$src" "$tmp" 2>/dev/null; then
      rm -f "$tmp"
      if [ ! -e "$src" ]; then
        rollback_mirror_relocation_if_needed "$relocated" "$relocated_from" "$dst"
        echo "Source rollout disappeared during mirror copy, skipping: $rel" >> "$LOG"
        record_suppressed_mirror_rel "$suppressed_list" "$rel"
        record_suppressed_mirror_rel "$suppressed_list" "$relocated_from"
        while IFS=$'\t' read -r duplicate_rel duplicate_path; do
          record_suppressed_mirror_rel "$suppressed_list" "$duplicate_rel"
        done < "$duplicate_index"
        continue
      fi
      status=1
      break
    fi

    complete_size=$(last_complete_line_size "$tmp")
    if [ "$complete_size" -eq 0 ]; then
      rm -f "$tmp"
      if [ "$relocated" -eq 1 ]; then
        record_committed_mirror_relocation "$relocated" "$relocation_list" "$relocated_from" "$rel"
        record_touched_mirror_file "$touched_list" "$dst"
      fi
      if [ -f "$dst" ]; then
        while IFS=$'\t' read -r duplicate_rel duplicate_path; do
          [ -f "$duplicate_path" ] || continue
          if duplicate_size=$(file_size_if_present "$duplicate_path"); then
            :
          else
            stat_status=$?
            case "$stat_status" in
              1)
                continue
                ;;
              *)
                echo "Failed to stat duplicate mirror rollout during sync: $duplicate_rel" >> "$LOG"
                status=1
                break
                ;;
            esac
          fi
          if [ "$duplicate_size" -eq "$dst_size" ] && cmp -s "$duplicate_path" "$dst"; then
            rm -f "$duplicate_path"
            echo "Mirror prune stale duplicate $duplicate_rel -> $rel" >> "$LOG"
            record_mirror_relocation "$relocation_list" "$duplicate_rel" "$rel"
          fi
        done < "$duplicate_index"
        if [ "$status" -ne 0 ]; then
          break
        fi
      fi
      continue
    fi

    truncate_file_to_size "$tmp" "$complete_size"
    if [ "$complete_size" -eq "$dst_size" ] && [ -f "$dst" ] && cmp -s "$tmp" "$dst"; then
      rm -f "$tmp"
      if [ "$relocated" -eq 1 ]; then
        record_committed_mirror_relocation "$relocated" "$relocation_list" "$relocated_from" "$rel"
        record_touched_mirror_file "$touched_list" "$dst"
      fi
      while IFS=$'\t' read -r duplicate_rel duplicate_path; do
        if [ -f "$duplicate_path" ]; then
          rm -f "$duplicate_path"
          echo "Mirror prune stale duplicate $duplicate_rel -> $rel" >> "$LOG"
        fi
        record_mirror_relocation "$relocation_list" "$duplicate_rel" "$rel"
      done < "$duplicate_index"
      continue
    fi

    mv "$tmp" "$dst"
    echo "Mirror update $rel ($dst_size -> $complete_size)" >> "$LOG"
    record_committed_mirror_relocation "$relocated" "$relocation_list" "$relocated_from" "$rel"
    while IFS=$'\t' read -r duplicate_rel duplicate_path; do
      if [ -f "$duplicate_path" ]; then
        rm -f "$duplicate_path"
        echo "Mirror prune stale duplicate $duplicate_rel -> $rel" >> "$LOG"
      fi
      record_mirror_relocation "$relocation_list" "$duplicate_rel" "$rel"
    done < "$duplicate_index"
    record_touched_mirror_file "$touched_list" "$dst"
  done < "$source_index"

  rm -f "$source_index" "$source_rel_index" "$mirror_index" "$stale_match_index" "$duplicate_index"
  return "$status"
}
