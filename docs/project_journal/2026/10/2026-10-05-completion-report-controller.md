---
id: 20261005-crb001
title: Diagnostic Completion Controller
status: completed
created: 2026-10-05
updated: 2026-10-05
branch: codex/completion-report-v217
pr: "https://github.com/Joey-Tools/codex-rollout-backup/pull/20"
supersedes: []
superseded_by:
---

# Diagnostic Completion Controller

## Summary

- Reject verifier filename lookalikes by accepting only the exact canonical path or GitHub PR merge-ref path (`@refs/pull/<number>/merge`).
- Preserve known-PR concurrency grouping; use run IDs only to isolate association-less runs, never as the action `pr_number`.

## Current State

- Before runner allocation, the controller admits only completed pull-request runs of the canonical verifier workflow, using its exact path or the GitHub PR merge-ref path `@refs/pull/<number>/merge`; workflow filename lookalikes are rejected. Zero or one associated pull request is accepted; multiple associations are rejected. An empty association passes the `0` sentinel to the runtime's separately verified fallback.
- Concurrency stays keyed by the associated PR, issue, or dispatch number first; workflow-run IDs are isolation fallbacks only when those keys are absent and never become the action's `pr_number`.
- Only CODEX_REVIEW_GATE_AUTO_REQUEST=true, attempt 1, failure, and one associated pull request select begin-review and an automatic review request. Other eligible completions select report-completion, including successful runs, reruns, and runs when automatic requests are disabled.
- report-completion updates diagnostic state only; it does not rerun or dispatch workflows, request a review, scan provider findings, or write the required check. The canonical verifier check remains the merge authority.
- Event subscriptions, permissions, runner selection, verifier workflow, CODEOWNERS, GitHub variables, and rulesets are unchanged. This implementation does not provide live settings or canary evidence.

## Next Steps

- No controller implementation follow-up is required. Any live canary or operational state must be established from separate evidence.

## Evidence

- Consumer baseline: 04cecdfd859fe74f0c0ce1f5a90a744ee4ef6d5d.
- Canonical source: 7e1069c6a6f4c4b319b1c5f33da262ee97460242; source PR #101 is merged and runtime v2.1.7 is published. Controller SHA-256: 2499fa48ec6a474f1509329ac3ce37c3e62914356de28cba60d02791fed78c0f.
- Hardening source HEAD: 97268b8a83213300182e9f699eb5cb4dba627670; canonical template blob: c6290c800903303151cbfb34ca706463118b0d09; template SHA-256: e0dd71ef69fb36249429eff1d0ba0e1fd425e3650a1bddec1aabbdfc45f3f665.
- cmp against the canonical completion-controller template, source bootstrap --prepare-worktree dry run, actionlint v1.7.12 on verifier and controller, and git diff --check passed.
- No controller-specific test was found in this repository; the focused workflow checks above passed.
