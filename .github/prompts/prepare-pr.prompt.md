---
description: Use when preparing a VirtRTLab branch for commit or pull request with conventional commits, labels, milestones, and reviewer-ready PR text
---

Prepare the change for Git and GitHub integration.

Then:

1. Identify the main domain and scope of the change.
2. Propose or create a branch name following the repo convention.
3. Propose conventional commit messages matching the actual diff.
4. Verify that `make check` passed.
5. Verify that the relevant QA targets passed:
	- `make qa` for CLI and daemon changes
	- `make qa-kernel-lint` when `kernel/**` changed
6. Verify that the pytest suites passed when run separately:
	- `python3 -m pytest -c pytest.ini tests/cli`
	- `python3 -m pytest -c pytest.ini tests/daemon`
	- `python3 -m pytest -c pytest.ini tests/kernel`
	- `python3 -m pytest -c pytest.ini tests/install`
7. Check whether docs or review steps are still missing.
8. Draft the PR title and body using the project template.

If the diff mixes unrelated concerns, or if QA and tests are not green, call that out before proposing a push or PR.