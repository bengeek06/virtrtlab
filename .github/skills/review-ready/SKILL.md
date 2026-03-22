---
name: review-ready
description: "Use when: preparing a VirtRTLab change for review by checking tests, contracts, commit hygiene, and reviewer expectations"
---

# Review Ready

Use this skill when a patch exists and you want to make it ready for human or agent review.

## Checklist

1. Confirm the change matches the current contract in README.md and docs/.
2. Run `make check`.
3. Run the relevant QA targets:
	- `make qa` for CLI and daemon changes
	- `make qa-kernel-lint` when `kernel/**` changes
4. Run the narrowest relevant domain tests.
5. Run the pytest suites separately before any PR:
	- `python3 -m pytest -c pytest.ini tests/cli`
	- `python3 -m pytest -c pytest.ini tests/daemon`
	- `python3 -m pytest -c pytest.ini tests/kernel`
	- `python3 -m pytest -c pytest.ini tests/install`
6. Check whether docs need updating.
7. Request the matching reviewer agent for the touched domain.
8. Address blockers and majors before asking for PR preparation or push for review.
9. Use git-github-expert for branch naming, commit wording, push readiness, and PR body.

## Domain mapping

- kernel/** -> kernel-reviewer
- daemon/** -> userspace-reviewer
- cli/** and tests/**/*.py -> python-reviewer
- docs/** and README.md -> spec-reviewer

## Expected outcome

- no obvious contract drift
- required QA passed
- reviewer-ready patch
- all pytest suites passed before PR
- conventional commit and PR scaffolding prepared