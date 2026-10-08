---
id: 20261008-rtr001
title: Review Request Token Input Rollout
status: active
created: 2026-10-08
updated: 2026-10-08
branch: codex/review-request-token-rollout
pr:
supersedes: []
superseded_by:
---

# Review Request Token Input Rollout

## Summary

- Pass the dedicated request-token secret to the consumer controller while preserving the existing GitHub token input.

## Current State

- The controller maps `secrets.CODEX_REVIEW_GATE_REQUEST_TOKEN` to `review_request_token` immediately after `github_token`.
- This workflow change creates or modifies no secret, permission, event subscription, runner, concurrency group, verifier, repository or organization variable, or ruleset.
- The consumer update is one slice of the parent multi-repository rollout.

## Next Steps

- Complete the corresponding consumer-input updates in the remaining repositories in the parent rollout.

## Evidence

- `.github/workflows/codex-review-gate-controller.yml`.
