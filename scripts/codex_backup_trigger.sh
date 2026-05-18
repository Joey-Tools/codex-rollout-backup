#!/bin/bash
set -euo pipefail

USER_DOMAIN="gui/$(id -u)"
SNAPSHOT_LABEL="${CODEX_SNAPSHOT_LABEL:-io.github.joey-tools.codex.snapshot.daily}"

launchctl kickstart -k "$USER_DOMAIN/$SNAPSHOT_LABEL"
