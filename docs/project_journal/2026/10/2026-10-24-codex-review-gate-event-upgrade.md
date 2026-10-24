---
id: 20261024-crb001
title: Codex Review Gate Event Upgrade
status: completed
created: 2026-10-24
updated: 2026-10-24
branch: master
pr:
supersedes: []
superseded_by:
---

# Codex Review Gate Event Upgrade

## Summary
- Upgrade the consumer verifier/controller pair to the canonical v2.2.12 workflows.

## Current State
- The verifier accepts `stacked` and `edited`; non-base-ref edits use isolated ignored titles and concurrency, while the controller only processes verifier-titled workflow runs.
- The workflow supports configurable runner labels while preserving the existing hosted-runner fallback. The `@v2` action pin, token wiring, auto-request behavior, and limits-profile behavior are unchanged; no repository settings were modified.
- The source pair matches the designated canonical files byte-for-byte. Static workflow validation and `git diff --check` passed. No live canary or runtime behavior is claimed.

## Next Steps
- Use a separate live canary before making operational claims about deployed behavior.

## Evidence
- `.github/workflows/codex-review-gate.yml`
- `.github/workflows/codex-review-gate-controller.yml`
