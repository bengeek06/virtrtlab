---
description: Use when implementing, fixing, or debugging virtrtlabd, epoll, AF_UNIX sockets, or runtime permission handling in daemon/
---

Analyse the affected daemon code and the relevant contracts in README.md, docs/daemon.md, docs/socket-api.md, and docs/privilege-model.md.

Then:

1. Identify the minimal correct change.
2. Update the implementation and the closest relevant tests.
3. Run `make check`, `make qa`, and `python3 -m pytest -c pytest.ini tests/daemon`.
4. Before any PR, run `python3 -m pytest -c pytest.ini tests/cli`, `python3 -m pytest -c pytest.ini tests/daemon`, `python3 -m pytest -c pytest.ini tests/kernel`, and `python3 -m pytest -c pytest.ini tests/install`.
5. Summarize behavior changes, validation, and residual risk.

If the contract is ambiguous, stop and propose a spec update before proceeding.