---
description: Use when implementing, fixing, or debugging VirtRTLab kernel modules, sysfs behavior, device lifecycle, or module load and unload paths in kernel/
---

Read the relevant contract first in README.md and the matching docs under docs/.

Then:

1. Read the affected kernel sources and build context before editing.
2. Make the smallest correct change that fixes the root cause.
3. Preserve Linux kernel coding style and current VirtRTLab naming rules.
4. Run `make check`, `make qa-kernel-lint`, and `python3 -m pytest -c pytest.ini tests/kernel`.
5. Before any PR, run `python3 -m pytest -c pytest.ini tests/cli`, `python3 -m pytest -c pytest.ini tests/daemon`, `python3 -m pytest -c pytest.ini tests/kernel`, and `python3 -m pytest -c pytest.ini tests/install`.
6. Summarize the behavioral change, validation, and residual risks.

If the contract is unclear or incomplete, stop and request a spec update before implementation.