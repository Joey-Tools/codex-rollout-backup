#!/bin/bash

onedrive_cli() {
  if [ -n "${ONEDRIVE_CLI_PATH:-}" ]; then
    if [ -x "$ONEDRIVE_CLI_PATH" ]; then
      printf '%s\n' "$ONEDRIVE_CLI_PATH"
      return 0
    fi

    return 1
  fi

  if [ -x /Applications/OneDrive.App/Contents/MacOS/OneDrive ]; then
    printf '%s\n' /Applications/OneDrive.App/Contents/MacOS/OneDrive
  elif [ -x /Applications/OneDrive.app/Contents/MacOS/OneDrive ]; then
    printf '%s\n' /Applications/OneDrive.app/Contents/MacOS/OneDrive
  else
    return 1
  fi
}

supports_onedrive_unpin() {
  local root_link="${ONEDRIVE_ROOT:-$HOME/OneDrive}"
  local resolved_root

  [ -L "$root_link" ] || return 1
  resolved_root=$(cd "$root_link" && pwd -P) || return 1

  case "$resolved_root" in
    "$HOME/Library/CloudStorage/"*) ;;
    *) return 1 ;;
  esac

  onedrive_cli >/dev/null
}

resolve_existing_path() {
  local path="$1"
  local dir

  dir=$(cd "$(dirname "$path")" && pwd -P) || return 1
  printf '%s/%s\n' "$dir" "$(basename "$path")"
}

onedrive_getpin_output() {
  local path="$1"
  local cli
  local resolved_path

  cli=$(onedrive_cli) || return 1
  resolved_path=$(resolve_existing_path "$path") || return 1

  "$cli" /getpin "$resolved_path" 2>&1 || true
}

wait_for_onedrive_ready() {
  local path="$1"
  local attempts="${2:-${ONEDRIVE_UNPIN_READY_ATTEMPTS:-10}}"
  local delay_seconds="${3:-${ONEDRIVE_UNPIN_READY_DELAY_SECONDS:-1}}"
  local attempt=1
  local output=""
  local resolved_path

  resolved_path=$(resolve_existing_path "$path") || return 1

  while [ "$attempt" -le "$attempts" ]; do
    output=$(onedrive_getpin_output "$resolved_path")
    if ! printf '%s' "$output" | grep -Fq 'status=-2'; then
      printf '%s\n' "$output"
      return 0
    fi

    echo "OneDrive /getpin not ready for $resolved_path (attempt $attempt/$attempts): $output" >> "$LOG"
    sleep "$delay_seconds"
    attempt=$((attempt + 1))
  done

  printf '%s\n' "$output"
  return 1
}

unpin_onedrive_copy() {
  local path="$1"
  local cli
  local resolved_path
  local pin_state_output
  local unpin_output
  local final_state
  local flags

  cli=$(onedrive_cli) || return 1
  resolved_path=$(resolve_existing_path "$path") || return 1

  pin_state_output=$(wait_for_onedrive_ready "$resolved_path") || {
    echo "Skipping OneDrive /unpin for $resolved_path: provider still reports status=-2 after readiness retries." >> "$LOG"
    return 0
  }

  if printf '%s' "$pin_state_output" | grep -Fq 'pin state=Unpinned'; then
    return 0
  fi

  unpin_output=$("$cli" /unpin "$resolved_path" 2>&1 || true)
  if [ -n "$unpin_output" ]; then
    printf '%s\n' "$unpin_output" >> "$LOG"
  fi

  flags=$(/usr/bin/stat -f%Sf "$resolved_path" 2>/dev/null || true)
  if printf '%s' "$flags" | grep -Fq 'dataless'; then
    return 0
  fi

  final_state=$(onedrive_getpin_output "$resolved_path")
  if printf '%s' "$final_state" | grep -Fq 'pin state=Unpinned'; then
    return 0
  fi

  echo "OneDrive /unpin did not evict $resolved_path yet; final getpin output: $final_state" >> "$LOG"
}
