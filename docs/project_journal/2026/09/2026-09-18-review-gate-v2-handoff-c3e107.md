---
id: 20260918-c3e107
title: Review Gate v2 Handoff
status: active
created: 2026-09-18
updated: 2026-09-18
branch: codex/organization-v2-handoff
pr:
supersedes: []
superseded_by:
---

# Review Gate v2 Handoff

## Summary
- Install the canonical v2 verifier and controller while retaining a temporary v1 legacy bridge.
- Protect the review-gate control plane with `@JoeyTeng` CODEOWNERS coverage.

## Current State
- Pull requests can produce both the existing `codex/review-gate` status and the new `codex/github-review-gate` check during the organization-wide handoff.
- The repository workflow uses the owner-approved floating `JoeyTeng/codex-review-gate-action@v2` selector.
- This repository change does not modify the organization ruleset, and the legacy bridge remains required until the cohort-wide v2 cutover is complete.

## Next Steps
- Verify the v2 check on the installed default-branch workflow as part of the full consumer cohort.
- After the organization ruleset requires v2 and no longer requires v1, remove the legacy bridge in a separate cleanup change.

## Evidence
- Canonical producer: `JoeyTeng/codex-review-gate-action@v2`.
- Source handoff implementation: https://github.com/Joey-Tools/codex-review-gate/pull/51.
