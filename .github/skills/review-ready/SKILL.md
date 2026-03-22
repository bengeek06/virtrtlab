---
name: review-ready
description: "Use when: preparing a VirtRTLab change for review by checking tests, contracts, commit hygiene, and reviewer expectations"
---

# Review Ready

Use this skill when a patch exists and you want to make it ready for human or agent review.

## Checklist

1. Confirm the change matches the current contract in README.md and docs/.
2. Run the narrowest relevant build and test targets.
3. Check whether docs need updating.
4. Request the matching reviewer agent for the touched domain.
5. Address blockers and majors before asking for PR preparation.
6. Use git-github-expert for branch naming, commit wording, and PR body.

## Domain mapping

- kernel/** -> kernel-reviewer
- daemon/** -> userspace-reviewer
- cli/** and tests/**/*.py -> python-reviewer
- docs/** and README.md -> spec-reviewer

## Expected outcome

- no obvious contract drift
- reviewer-ready patch
- conventional commit and PR scaffolding prepared