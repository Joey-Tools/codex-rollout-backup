---
id: 20261018-whole-rollout-tree-snapshots
title: Whole Rollout Tree Snapshots
status: completed
created: 2026-10-18
updated: 2026-10-18
branch: wip/whole-rollout-trees
pr: https://github.com/Joey-Tools/codex-rollout-backup/pull/23
supersedes: []
superseded_by:
---

# Whole Rollout Tree Snapshots

## Summary

- Archive the complete `sessions/` and `archived_sessions/` trees directly from `CODEX_ROOT`, including compressed `.zst` rollouts and any future file formats.
- Preserve raw file bytes, hidden files, empty directories, and child symlinks without filename filtering, rollout-ID processing, JSONL parsing, or incomplete-line trimming.
- Leave the existing local JSONL mirror, repair/retry state, and historical snapshots in place.

## Decisions

- Archive whichever source trees exist; retain empty trees and skip when both are absent. Do not fall back to historical mirror contents.
- Reject symlink or non-directory top-level sources and outputs nested inside a source tree before creating lock state or archive output.
- Reserve both source paths for containment checks even while they are absent, so output or state creation cannot create and pollute a future source tree.
- Keep the snapshot-wide lock, staging-directory object-identity checks, compression, publish retry/recovery, OneDrive eviction, per-computer destination, and daily 02:00 schedule.
- Keep legacy JSONL mirror/repair tools available for manual use; daily snapshots no longer invoke them.
- Read the live source trees with tar. This does not promise an atomic whole-tree point in time or a complete final JSONL line. A reported tar or compressor failure stops publication.

## Validation

- Python 3.12.12: all 416 existing repair and mirror-copy tests passed.
- All 37 shell integration cases passed, including byte-for-byte opaque files, partial JSONL, unusual filenames, empty trees, symlinks, missing sources, preserved legacy mirror contents, and failure-safe publication.
- Pre-lock regression cases reject snapshot, staging, and state paths under absent source trees without creating those trees.
- Individual `bash -n` checks, Python compilation, project-journal validation, and whitespace checks passed. ShellCheck was unavailable locally; syntax and integration checks supplied the executed fallback.
- Replaced obsolete daily mirror/recovery assertions with complete-tree archive cases while retaining standalone mirror/helper coverage.

## Next Steps

- None for whole-tree snapshot support.
