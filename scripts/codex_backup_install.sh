#!/bin/bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SNAPSHOT_LABEL="${CODEX_SNAPSHOT_LABEL:-io.github.joey-tools.codex.snapshot.daily}"
SNAPSHOT_SCRIPT="$SCRIPT_DIR/codex_snapshot_daily.sh"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
USER_DOMAIN="gui/$(id -u)"
SNAPSHOT_LINK="$LAUNCH_AGENTS_DIR/$SNAPSHOT_LABEL.plist"
SNAPSHOT_STDOUT="${CODEX_SNAPSHOT_STDOUT:-$HOME/Library/Logs/codex_snapshot_daily.out}"
SNAPSHOT_STDERR="${CODEX_SNAPSHOT_STDERR:-$HOME/Library/Logs/codex_snapshot_daily.err}"
SNAPSHOT_PATH="${CODEX_SNAPSHOT_PATH:-/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin}"
SNAPSHOT_LEGACY_LABELS="${CODEX_SNAPSHOT_LEGACY_LABELS:-}"
SNAPSHOT_ENV_KEYS=(
  CODEX_SNAPSHOT_DIR
  CODEX_ROOT
  CODEX_BACKUP_BASE
  CODEX_MIRROR_ROOT
  CODEX_BACKUP_STATE_ROOT
  CODEX_SNAPSHOT_STAGING_DIR
  CODEX_SNAPSHOT_PUBLISH_RENAME_ATTEMPTS
  CODEX_SNAPSHOT_PUBLISH_RENAME_DELAY_SECONDS
  ONEDRIVE_ROOT
  ONEDRIVE_CLI_PATH
  ONEDRIVE_UNPIN_READY_ATTEMPTS
  ONEDRIVE_UNPIN_READY_DELAY_SECONDS
)

xml_escape() {
  printf '%s' "$1" | sed \
    -e 's/&/\&amp;/g' \
    -e 's/</\&lt;/g' \
    -e 's/>/\&gt;/g' \
    -e 's/"/\&quot;/g' \
    -e "s/'/\&apos;/g"
}

write_snapshot_plist() {
  local label script stdout stderr

  label=$(xml_escape "$SNAPSHOT_LABEL")
  script=$(xml_escape "$SNAPSHOT_SCRIPT")
  stdout=$(xml_escape "$SNAPSHOT_STDOUT")
  stderr=$(xml_escape "$SNAPSHOT_STDERR")

  {
    cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$label</string>
    <key>ProgramArguments</key>
    <array>
      <string>/bin/bash</string>
      <string>$script</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
      <key>Hour</key><integer>2</integer>
      <key>Minute</key><integer>0</integer>
    </dict>
EOF
    write_snapshot_environment
    cat <<EOF
    <key>StandardOutPath</key><string>$stdout</string>
    <key>StandardErrorPath</key><string>$stderr</string>
</dict>
</plist>
EOF
  } > "$SNAPSHOT_LINK"
}

write_snapshot_environment() {
  local key escaped_key escaped_value

  printf '    <key>EnvironmentVariables</key>\n'
  printf '    <dict>\n'
  printf '      <key>PATH</key><string>%s</string>\n' "$(xml_escape "$SNAPSHOT_PATH")"
  for key in "${SNAPSHOT_ENV_KEYS[@]}"; do
    [ -n "${!key:-}" ] || continue
    escaped_key=$(xml_escape "$key")
    escaped_value=$(xml_escape "${!key}")
    printf '      <key>%s</key><string>%s</string>\n' "$escaped_key" "$escaped_value"
  done
  printf '    </dict>\n'
}

cleanup_legacy_jobs() {
  local legacy_label
  local legacy_labels=()

  [ -n "$SNAPSHOT_LEGACY_LABELS" ] || return 0
  read -r -a legacy_labels <<< "$SNAPSHOT_LEGACY_LABELS"
  for legacy_label in "${legacy_labels[@]}"; do
    [ -n "$legacy_label" ] || continue
    launchctl bootout "$USER_DOMAIN/$legacy_label" 2>/dev/null || true
    rm -f "$LAUNCH_AGENTS_DIR/$legacy_label.plist"
  done
}

mkdir -p "$LAUNCH_AGENTS_DIR"

cleanup_legacy_jobs

write_snapshot_plist

plutil -lint "$SNAPSHOT_LINK"

launchctl bootout "$USER_DOMAIN/$SNAPSHOT_LABEL" 2>/dev/null || true
launchctl bootstrap "$USER_DOMAIN" "$SNAPSHOT_LINK"

launchctl print "$USER_DOMAIN/$SNAPSHOT_LABEL"
