---
id: 20260817-01a00f
title: Reflink Repair and Snapshot Temp Retention
status: completed
created: 2026-08-17
updated: 2026-08-18
branch:
pr: https://github.com/Joey-Tools/codex-rollout-backup/pull/6
supersedes: []
superseded_by:
---

# Reflink Repair and Snapshot Temp Retention

## Summary
- Route ordinary rollout refreshes through a held-FD private adjacent stage so APFS strict clone can create a new destination, with ordinary copy allowed only for classified filesystem incompatibility.
- Before creating a new local zstd staging file, remove older matching `codex-rollouts-*.tar.zst.tmp.*` files under the shared snapshot lock; abort before creating the new file if cleanup fails.
- Add a tracked, macOS-only, fail-closed repair utility for existing byte-identical mirror files, with crash recovery and a persistent retry queue for unstable rollouts.

## Decisions
- Protect rollout byte content and access policy during repair; destination inode, ctime, and birth time may change as an intentional consequence of replacement.
- Match source and mirror rollouts by rollout identifier across `sessions` and `archived_sessions`, and revalidate every candidate immediately before replacement.
- Use strict `fclonefileat` cloning without an ordinary-copy fallback, then publish with `renameatx_np(RENAME_SWAP)` and rollback on failed post-verification.
- Require every possible final mirror survivor to remain euid-owned, free of group/other write bits and extended ACLs, and limited to safe user-settable BSD flags. Treat an initial violation as unsupported/terminal rather than retryable.
- Persist per-file recovery state before swapping, and retain distinct outcomes for missing, unreadable, mismatched, policy-changed, and unstable files.
- Persist `PLANNED`, `STAGE_BOUND`, and `CLONE_BOUND` intent fences before `PREPARED`; recover only exact safe stage/object states, prove the original survivor before cleanup, and retain ambiguous or nonempty evidence.
- Publish an enabled retry obligation before deleting the last intent or manifest fence, including identity-bound rollback to `DEFERRED` when a `PREPARED` source path moves or disappears.
- Queue unstable rollouts by identifier so the next daily run can relocate and retry them after an archive-state move.
- Harden repair control state as owner-private directories/files, retry only benign mtime/ctime JSON metadata-generation churn, and fail closed on size/content, identity, access-policy, or persistent-generation instability.
- Treat each batch command as a sequence of recoverable per-file transactions, not as one global transaction. Preserve completed results and report `completed_before_fatal` when a later candidate fails fatally.
- Route ordinary mirror refreshes through a held-FD macOS helper that rejects symlink/FIFO/replacement inputs, prefers strict clone, and permits `fcopyfile` only for classified clone-filesystem incompatibility; keep the repair path strict-clone-only.
- Require the ordinary helper's source direct parent and destination container to be euid-owned, non-group/world-writable, ACL-free, and limited to safe flags; retain the same canonical-namespace stability assumption as the snapshot lock for the final check-to-syscall window.
- Capture source/mirror inventories completely as NUL-delimited data before consuming them, propagate producer failures, reject noncanonical or delimiter-unsafe roots/relative paths, keep suppression/archive lists NUL-safe, and clean owner-private index directories without replacing the primary exit status.
- Allow a strict-cloned active source to append only when the same held object preserves a cross-validation observed-size high-water and stable mtime/ctime generation, its access policy stays fixed, and the staged bytes remain a verified newline-terminated source prefix. Permit generation change only with size growth; defer same-size rewrite/generation churn and any later shrink, including truncation of only an incomplete suffix. Require a fully stable source snapshot around an ordinary-copy fallback.
- Return `unchanged` or `no-complete-line` only after identity-bound stage cleanup and a final held-candidate/source-prefix/path/destination revalidation. Once a resource is Python-visible and control has entered the owner/caller's first guarded body point, require guarded acquire/handoff to register it atomically in an active enumerable owner or drain it safely. Preserve the original primary error object/type/traceback across one asynchronous `BaseException` during guarded acquire, handoff, operation, or first-round cleanup, then perform a bounded second drain; aggregate independently actionable cleanup failures when no primary is active.
- Bind any retried pre-publication cleanup to the originally captured parent/name/object identity, and report terminal state only after proving the namespace durability disposition.
- When a publish failure leaves both the before and published namespace orientations unproved, skip cleanup and report `cleanup skipped; orientation unverified`; do not claim retained stage evidence across the excluded same-euid namespace window.
- Run durable recovery before ordinary mirror sync, then process the retry queue before archive creation; a recovery fatal blocks the rest of the daily run.
- Pause the daily launchd job and verify a quiet process window before modifying existing mirror files.
- Hold a separate snapshot-wide protocol lock across mirror sync, retry, compression, publish, and unpin. This serializes the zstd rotation/cap path but does not replace the quiet-window requirement for one-off repair.
- Require the configured snapshot staging path to resolve to a non-symlink directory. Reject an existing symlink or non-directory before touching its target; create a missing directory, bind its `st_dev:st_ino` identity, and revalidate that identity before zstd cleanup, zstd temporary creation, and gzip temporary creation. Retain the existing same-euid final-check-to-syscall exclusion.
- Limit the ordinary mirror helper's hardening claim to source read, staging, copy, and publish. Surrounding shell `mkdir -p`, relocation, and duplicate-prune operations, plus malicious same-euid replacement after a final name check, remain outside the held-FD guarantee.
- Bound pure-Python resource ownership after the native return value is Python-visible and control enters the owner/caller's first guarded body point. Deterministic line-trace injection is only a test proxy for one asynchronous `BaseException`; tracing `call`/`return` events, finite nested function-entry boundaries before any cleanup guard, repeated/re-entrant tracing or profiling, unbounded cancellation, and interpreter shutdown are excluded, and `__del__` is best-effort rather than a correctness mechanism. An interruption after kernel/C-call success but before the return value is handed to Python also cannot be identity-bound closed; do not scan `/dev/fd` or claim native signal masking.
- Run a fresh inventory in dry-run mode, apply a small representative canary, and obtain Joey's explicit confirmation before the full repair.
- Treat the mirror and retry result as authoritative after the repair. Judge OneDrive publication independently; a publication failure must not invalidate a successful mirror repair.
- Remove all older matching zstd staging files before creating a new zstd archive, including a complete staging file preserved after a prior publish failure; abort before creation if cleanup fails. Do not remove publish temporary files or gzip staging files.
- This repository uses squash merge; tracked journal state describes the post-merge `master` state rather than transient review, branch, or PR status.

## Current State
- The earlier Codex task established that pre-creating the reflink destination forced every mirror update through the ordinary-copy fallback.
- The target `master` state routes ordinary refreshes through a held-FD private adjacent stage, performs durable recovery before sync, retries queued repair work before archive creation, rotates only matching zstd staging files, and holds a snapshot-wide lock across the complete daily snapshot lifecycle.
- The target state includes the repair CLI, ordinary mirror helper, shared Darwin backend, snapshot lock supervisor, shell/Python/real-Darwin regression coverage, CI, and operator documentation.
- PR #6 was squash-merged to `master` as `dafb10af6090201117267bc076f7bd9b562aa5e4`. The authorized production full repair and scheduled-path verification are complete, so this workstream is completed.
- The canonical mirror is clean, the scheduled job is loaded and idle after one successful run, no zstd staging temporary remains, and the persistent retry queue contains 640 classified deferrals for future scheduled retries.

## Progress Notes
- During integration, a narrow smoke probe accidentally ran from the prepared worktree between `2026-08-17T17:36:03.395Z` and `2026-08-17T17:36:33.639Z` as `bash -c '. ./scripts/codex_rollout_mirror_common.sh; sync_codex_rollout_mirror'`. It omitted the intended isolation, inherited `HOME=/Users/hoteng` and `TMPDIR=/var/folders/wx/p3kml5gn14s_5k6xp15qjtxc0000gn/T/`, and left `LOG` plus every `CODEX_*` override unset. The command therefore selected the default production source/mirror configuration; it was not authorized to target production. The outer tool call reported success and no output, but it did not print the nested sync function's exit status, so that status is unknown rather than proved `0`.
- The follow-up read-only check found zero production mirror/state files or directories changed in the twenty-minute window across all four ctime/mtime checks, no `.codex-rollout-stage.*` entry, mirror-root ctime/mtime still at `2026-03-09`, and state-root ctime/mtime still at `2026-05-28`. Discovery produced no changed candidate, the current shell no longer performs a pre-helper `mv`, and the empty `LOG` redirection would have failed before a helper exec if that branch had been reached; neither held-FD copy/publish nor relocation/pruning ran. The only writes were five indexes created and then cleaned under the inherited `TMPDIR`. This evidence supports read-only enumeration/stat with no production mirror/state mutation from that mistaken invocation; it does not retroactively authorize it. All subsequent tests explicitly isolate `HOME`, `CODEX_*`, and `LOG`.
- A real-macOS shell test exposed an ACL gate bug: Darwin `acl_get_entry` reports a successfully returned first entry with status `0`, while the prior check expected `1`. The fix treats status `0` plus a non-null entry as proof that an extended ACL exists and fails closed for every other non-null ACL result. The frozen real-Darwin/APFS regressions and both complete Python suites now exercise this gate and pass on Python 3.9.6 and 3.13.0.
- After Joey's explicit confirmation, the authorized full repair and the next scheduled-path snapshot both completed successfully. The remaining 640 queue entries are classified deferred work, not an incomplete or fatal batch.
- The scheduled run created one Python bytecode cache entry in the canonical mirror. That exact residue was removed and the mirror was proved clean; the production-completion follow-up now exports `PYTHONDONTWRITEBYTECODE=1` before any Python invocation so the full scheduled child process tree inherits bytecode suppression.

## Next Steps
- None for this completed workstream.

## Evidence
- Prior Codex task: `019e4f9b-d3cb-7c92-9637-722ebb48c3db`.
- Prior rollout artifact: `/Users/hoteng/.codex/sessions/2026/05/22/rollout-2026-05-22T13-14-32-019e4f9b-d3cb-7c92-9637-722ebb48c3db.jsonl`.
- Canonical repository: `Joey-Tools/codex-rollout-backup`.
- Development base: `6139940769b6239103cf2c13b63fe6352b62f4fc`.
- Merged delivery: [PR #6](https://github.com/Joey-Tools/codex-rollout-backup/pull/6), squash commit `dafb10af6090201117267bc076f7bd9b562aa5e4`.
- Target-state paths: `.github/workflows/ci.yml`, `README.md`, `scripts/README.md`, `scripts/codex_rollout_mirror_common.sh`, `scripts/codex_snapshot_daily.sh`, `scripts/codex_snapshot_lock.py`, `scripts/codex_repair_rollout_reflinks.py`, `scripts/codex_reflink_darwin.py`, `scripts/codex_rollout_mirror_copy.py`, `tests/test_codex_launchd_scripts.sh`, `tests/test_codex_repair_rollout_reflinks.py`, `tests/test_codex_rollout_mirror_copy.py`, `docs/reflink-repair.md`, and this journal entry.
- Frozen production/test SHA-256 tuple:

  ```text
  5bc90ea52aef6097647443bd0325267ee8ba3dfa53c3ba168e9f47769a382b6d  .github/workflows/ci.yml
  abcc4b60c6b50fc9cf93dc260a692316efb1ceaee033d2afe071d4361a8d4261  scripts/codex_reflink_darwin.py
  ee73b6bf4ee035d4f734cda57294a9c8704e5a972a3427ac7191fcc6bfa3bd23  scripts/codex_repair_rollout_reflinks.py
  4cbcb227d4b344b6cc0623561d76b511b006aeaa1d52ce2312ee475ca217fee0  scripts/codex_rollout_mirror_copy.py
  0ed4179515771d6e4ea6e1ed785645cd2b812281e1f5408782ca8eb8add6df0c  scripts/codex_snapshot_lock.py
  36c1a2baec2ccd5bef78d94bf029133d0bd81b3b2ca32716b9154d786c381ee5  scripts/codex_rollout_mirror_common.sh
  6326ef40e66aad13b4a5f24dced20a751ae19aa0fccb21fe87032614a9706b54  scripts/codex_snapshot_daily.sh
  bc842f5617c60261fe88be5439577c3dbe78d3b34da0250c52971d2e5d34ec22  tests/test_codex_repair_rollout_reflinks.py
  d6c810b428a7e698f6fb5e081290cd997d5c3e56b7c364acc1f105dbc45d0598  tests/test_codex_rollout_mirror_copy.py
  25e2a9deb166db5112e09409fc767b5dbd305e3512c557c1e0495ddbcf829382  tests/test_codex_launchd_scripts.sh
  ```

- On both Python 3.9.6 and 3.13.0, the frozen mirror suite passed `189/189` and the frozen repair suite passed `227/227`. The real-Darwin/APFS subgroup passed `77/77`, the single asynchronous-interruption trace subgroup passed `65/65`, and the core union regression subgroup passed `11/11` on each interpreter. Fixtures ran under `/private/tmp` on the APFS Data volume.
- Both interpreters passed `py_compile`; Ruff `0.13.2` check and format-check, tracked diff checks, the six new untracked Python/test no-index whitespace checks, and pre/post hash checks all passed. Frozen backend, repair core, and ordinary mirror helper received fresh read-only audits with no P0-P2 finding; the helper audit started with zero task history. The earlier common/snapshot audit also reported no P0-P2 finding.
- Before PR #6 merged, the isolated shell suite passed all 42 top-level cases in about 114 seconds at snapshot-script SHA-256 `5f6fe393997ceae4e969b7b0225708089917d1d29de3a5411068378c467de0dc` and shell-test SHA-256 `cd1d57b57bf0f422336344b2561b8cd77c96b70834d4b421328cf9d1a4a96fa1`. Coverage included rejecting a symlink staging path without touching or adding any `*.tmp.*` under its target. `bash -n`, ShellCheck `0.11.0 -x`, tracked diff checks, pre/post hashes, and residue checks passed; the isolated harness did not execute production or `launchd` paths.
- The production-completion hygiene follow-up changed only the snapshot entrypoint and shell test to the final hashes above. The old production bytes failed the new selector with exit `97` and `Expected PYTHONDONTWRITEBYTECODE=1, got: 0`; the final selector and the adjacent missing-source and recover-before-sync-order selectors passed. The same-hash isolated full shell suite then passed all 43 cases in about 161 seconds under a bounded PTY; its 1,026-byte log contained only four expected synthetic publish-failure lines, and it did not execute production or `launchd` paths. `bash -n` passed the snapshot, shell-test, and common-sync files; ShellCheck `0.11.0 -x` passed the same three files; the full-worktree diff check passed. Pre/post/cleanup hashes were stable, no harness, `__pycache__`, `.pyc`, or `.pyo` residue remained, and the task log was cleaned exactly.
- The authorized full apply returned `status=ok`, `complete=true`, and `scan_errors=0`: it selected 28,247 files totaling 68,054,449,111 bytes, repaired 23,258, classified 4,989 as unstable, and published a retry queue of 5,629 entries.
- The scheduled run started at `Tue Aug 18 13:34:44 BST 2026` and exited `0`. Recovery applied zero transactions; retry applied all 5,629 queued entries, repaired 4,989, and deferred 640 as `active-complete-prefix=28`, `mirror-missing=220`, and `source-missing=392`, with `complete=true` and `scan_errors=0`.
- The scheduled run published OneDrive archive `codex-rollouts-2026-08-18.tar.zst` at 16,917,268,284 bytes with `compressed,dataless` flags. It rotated the old matching zstd temporary, observed at most one new matching temporary, and finished with zero matching staging temporaries.
- Post-run recovery dry-run returned zero transactions. Retry dry-run returned the persistent 640-entry queue as `active-complete-prefix=27`, `mirror-missing=220`, `source-missing=392`, and `unstable=1`, with `complete=true` and `scan_errors=0`. The canonical mirror was clean; launchd was loaded and idle with `active count=0`, `runs=1`, and `last exit code=0`.
- Production receipts are retained in owner-private directory `/Users/hoteng/Program/GitHub/Joey-Tools/codex-workspace/.codex-local/daily-skill-friction/production-evidence/20260818-01a00f-reflink-repair`:

  ```text
  e9505d181a93deb280a4f1667cfeaa92c77ef0539df7e8486850a092a5ef3354  launchd-run-20260818T133444.log
  399b72c6aca93b1926538ae6f9a5ee79d3168db6f06589166f493384f2e2c04f  launchd-terminal.txt
  9d81abd5da0017aea29acbec0f869803c7c0e6967b0ab036db2d4e141d594ba3  snapshot-final-stat.txt
  3663131316f96d20486fe7248d55c053839f573237ee2ae69566362f9c7f44b9  post-scheduled-recover.json
  58f0f491d3494cccdb662fa87d77b31b70229ec385eb5632e4a7c1280d0c1624  post-scheduled-retry.json
  ```
