---
name: feature-delivery
description: "Use when: delivering a new VirtRTLab feature end-to-end across spec, implementation, review, tests, and PR preparation"
---

# Feature Delivery

Use this skill for multi-step feature work that crosses documentation, implementation, validation, and GitHub hygiene.

## Recommended flow

1. Classify the change domain:
   - kernel
   - userspace C daemon
   - Python CLI or tests
   - cross-cutting spec only
2. Start with spec-author if the contract is unclear or changes.
3. Request spec-reviewer before implementation when the contract is non-trivial.
4. Implement with the matching dev agent.
5. Request the matching reviewer agent.
6. Finish with git-github-expert for branch, commits, and PR text.

## Expected outcome

- coherent contract
- minimal implementation
- relevant tests updated
- review findings addressed
- branch and PR ready

## Anti-patterns

- implementing against an ambiguous contract
- mixing kernel, daemon, and CLI changes without a documented reason
- opening a PR before review findings are resolved