---
description: Use when implementing or reviewing virtrtlabctl behavior, Python fixtures, pytest tests, or Python tooling in cli/ and tests/
---

Read the affected Python code and tests first.

Then:

1. Preserve documented CLI behavior unless the task explicitly changes it.
2. Make the smallest correct code change.
3. Add or update targeted tests.
4. Run `make check`, `make qa-cli`, and `python3 -m pytest -c pytest.ini tests/cli`.
5. Before any PR, run `python3 -m pytest -c pytest.ini tests/cli`, `python3 -m pytest -c pytest.ini tests/daemon`, `python3 -m pytest -c pytest.ini tests/kernel`, and `python3 -m pytest -c pytest.ini tests/install`.
6. Report the user-visible outcome and any assumptions about privileges or environment.

If the docs and the code disagree, note the mismatch explicitly.