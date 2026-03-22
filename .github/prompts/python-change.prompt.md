---
description: Use when implementing or reviewing virtrtlabctl behavior, Python fixtures, pytest tests, or Python tooling in cli/ and tests/
---

Read the affected Python code and tests first.

Then:

1. Preserve documented CLI behavior unless the task explicitly changes it.
2. Make the smallest correct code change.
3. Add or update targeted tests.
4. Run the smallest relevant pytest subset.
5. Report the user-visible outcome and any assumptions about privileges or environment.

If the docs and the code disagree, note the mismatch explicitly.