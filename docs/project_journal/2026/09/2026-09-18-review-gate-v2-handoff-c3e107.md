---
id: 20260918-c3e107
title: Review Gate v2 Handoff
status: completed
created: 2026-09-18
updated: 2026-09-29
branch: codex/daily-skill-friction-2026-09-29-codex-rollout-backup-remove-v1-bridge
pr:
supersedes: []
superseded_by:
---

# Review Gate v2 Handoff

## Summary
- Keep the canonical v2 verifier and controller and remove the temporary v1 legacy bridge after the organization-wide cutover.
- Protect the review-gate control plane with `@JoeyTeng` CODEOWNERS coverage.

## Current State
- Pull requests use the `codex/github-review-gate` check; the temporary `codex/review-gate` status producer has been removed.
- The verifier and controller use the owner-approved floating `JoeyTeng/codex-review-gate-action@v2` selector and the canonical `any` request-author policy.
- The controller responds to new provider comments; manual reconcile remains available for edited comments.
- This repository change does not modify the organization ruleset.

## Next Steps
- Use the v2 gate on each pull request and request a separate full organization audit after the freeze, a relevant configuration change, or before another rollout.

## Evidence
- Canonical producer: `JoeyTeng/codex-review-gate-action@v2`.
- Source handoff implementation: https://github.com/Joey-Tools/codex-review-gate/pull/51.
- Frozen post-cutover receipt SHA-256: `9a8b38f2188a14168423a07639d6662c87e198fe2dd12041f67fc224f363817e`.
- Receipt-only cleanup source: https://github.com/Joey-Tools/codex-review-gate/pull/89.
