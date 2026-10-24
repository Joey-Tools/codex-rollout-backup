---
id: 20261024-crb13a
title: Codex Review Gate v2.2.13 Consumer Update
status: completed
created: 2026-10-24
updated: 2026-10-24
branch: wip/non-skipping-review-gate
pr:
supersedes: []
superseded_by:
---

# Codex Review Gate v2.2.13 Consumer Update

## Summary

- This consumer's verifier workflow adopts the reviewed v2.2.13 source; the post-merge target-branch state is the byte-identical canonical verifier/controller pair.

## Current State

- The verifier uses the canonical fixed run title, required job name, and pull-request-scoped concurrency group. Its local ignored-edit run-name/concurrency/job-name overrides and job-level filter are removed.
- The controller remains unchanged. Permissions, runner-label override/default, floating action `@v2`, token, and variable interfaces are preserved.
- The local canonical-pair contract validator and `git diff --check` pass. This is source-level parity only; no consumer-specific live workflow pass or runtime canary is claimed.

## Next Steps

- No further local consumer-file work remains.

## Evidence

- [Source PR #146](https://github.com/Joey-Tools/codex-review-gate/pull/146)
- [Action release v2.2.13](https://github.com/JoeyTeng/codex-review-gate-action/releases/tag/v2.2.13)
- `.github/workflows/codex-review-gate.yml`
- `.github/workflows/codex-review-gate-controller.yml`
