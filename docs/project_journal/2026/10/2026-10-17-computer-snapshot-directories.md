---
id: 20261017-computer-snapshot-directories
title: Per-computer Snapshot Directories
status: completed
created: 2026-10-17
updated: 2026-10-17
branch: wip/computer-snapshots
pr:
supersedes: []
superseded_by:
---

# Per-computer Snapshot Directories

## Summary

- Isolate default OneDrive daily archives by the macOS ComputerName so different computers do not replace the same daily archive after a migration.
- Resolve the computer name on each run, with an explicit name override for computers that share a display name.
- Preserve explicit snapshot-directory overrides and leave existing shared archives in place.

## Decisions

- Use `~/OneDrive/Backup/dotfiles/codex/<ComputerName>/snapshots` as the default archive directory.
- Reject missing or unsupported names before creating a snapshot directory; do not silently combine distinct names through sanitization or a shared fallback.
- Keep the local rollout mirror, repair state, and snapshot lock locations unchanged.
- Retain the existing daily 02:00 schedule.

## Validation

- Python 3.12.12: all 416 repair and mirror-copy tests passed.
- The complete shell suite passed all 46 top-level cases, including runtime computer-name changes, separate same-day archives, explicit destination compatibility, invalid-name rejection, and installer environment propagation.
- All shell files passed individual `bash -n` checks; Python sources and tests passed compilation; project-journal validation and whitespace checks passed.
- ShellCheck was unavailable locally; syntax checks and the full shell suite supplied the executed fallback validation.
- The migration audit reproduced a native `os.dup2(..., inheritable=False)` crash in the unchanged baseline under the host's system Python 3.9.6. The independently installed Python 3.12.12 passed that case and the complete suite; the host backup installation selects that runtime through its existing `CODEX_SNAPSHOT_PATH` setting.

## Next Steps

- None for the implemented directory-isolation behavior.
