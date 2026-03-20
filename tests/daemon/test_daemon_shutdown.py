# SPDX-License-Identifier: MIT

"""
test_daemon_shutdown.py — shutdown tests for virtrtlabd (issue #10).

Acceptance criteria covered:
  AC4 — shutdown cleans up sockets and exits cleanly

Each test starts the daemon independently (not via daemon_proc fixture) so
that shutdown behaviour can be asserted on returncode and socket presence.
"""

import os
import subprocess
import time

import pytest

from conftest import DAEMON_BIN, RUN_DIR, SOCK_PATH, _run


def _start_daemon():
    """Launch virtrtlabd and wait up to 2 s for its socket to appear."""
    subprocess.run(["sudo", "rm", "-f", SOCK_PATH], check=False)

    proc = subprocess.Popen(
        ["sudo", DAEMON_BIN, "--num-uarts", "1", "--run-dir", RUN_DIR],
    )

    deadline = time.monotonic() + 2.0
    while not os.path.exists(SOCK_PATH):
        if time.monotonic() > deadline:
            proc.terminate()
            proc.wait(timeout=3)
            pytest.fail(f"Daemon did not create {SOCK_PATH} within 2 s")
        time.sleep(0.05)

    return proc


class TestDaemonShutdown:
    """virtrtlabd exits with code 0 and removes its sockets on signal."""

    def test_sigterm_removes_socket(self, uart_module):
        """
        SIGTERM must cause the daemon to:
          - exit with returncode 0
          - remove /run/virtrtlab/uart0.sock
        """
        if not os.path.exists(DAEMON_BIN):
            pytest.skip(f"Daemon not built: {DAEMON_BIN}")

        proc = _start_daemon()

        _run(["kill", "-TERM", str(proc.pid)], check=False)

        try:
            ret = proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            pytest.fail("virtrtlabd did not exit within 3 s after SIGTERM")

        assert ret == 0, \
            f"Expected returncode 0 after SIGTERM, got {ret}"
        assert not os.path.exists(SOCK_PATH), \
            f"{SOCK_PATH} must be removed on clean shutdown"

    def test_sigint_removes_socket(self, uart_module):
        """
        SIGINT (Ctrl-C equivalent) must trigger the same clean shutdown as
        SIGTERM: returncode 0 and socket removed.
        """
        if not os.path.exists(DAEMON_BIN):
            pytest.skip(f"Daemon not built: {DAEMON_BIN}")

        proc = _start_daemon()

        _run(["kill", "-INT", str(proc.pid)], check=False)

        try:
            ret = proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            pytest.fail("virtrtlabd did not exit within 3 s after SIGINT")

        assert ret == 0, \
            f"Expected returncode 0 after SIGINT, got {ret}"
        assert not os.path.exists(SOCK_PATH), \
            f"{SOCK_PATH} must be removed after SIGINT"
