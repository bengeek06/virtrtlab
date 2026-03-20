# SPDX-License-Identifier: MIT

"""
test_daemon_reconnect.py — reconnect test for virtrtlabd (issue #10).

Acceptance criteria covered:
  AC3 — a client can reconnect without restarting the daemon

Scenario:
  1. s1 connects, exchanges bytes, closes.
  2. Daemon drains stale bytes (DRAINING state), returns to WAIT_CLIENT.
  3. s2 connects and exchanges bytes — daemon PID unchanged throughout.
"""

import os
import select
import socket
import time

import pytest

from conftest import SOCK_PATH, open_tty_raw, restore_tty


class TestDaemonReconnect:
    """A simulator can disconnect and reconnect without restarting virtrtlabd."""

    def test_reconnect_without_restart(self, daemon_proc):
        """
        Two sequential simulator connections to the same socket must both
        succeed and relay bytes correctly.  The daemon process must not exit
        between the two sessions.
        """
        tty_fd, saved = open_tty_raw()
        try:
            # ---- First session -------------------------------------------
            s1 = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s1.connect(SOCK_PATH)

            s1.sendall(b"X")
            r, _, _ = select.select([tty_fd], [], [], 1.0)
            assert r, "TTY not readable during first session"
            os.read(tty_fd, 256)   # consume; content not checked here

            s1.close()

            assert daemon_proc.poll() is None, \
                "Daemon exited unexpectedly after first client disconnect"

            # Give the daemon time to complete the DRAINING transition.
            time.sleep(0.3)

            # ---- Second session (reconnect) --------------------------------
            s2 = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                s2.connect(SOCK_PATH)
            except (ConnectionRefusedError, OSError) as exc:
                pytest.fail(
                    f"Second connect() failed — daemon did not return to "
                    f"WAIT_CLIENT state after drain: {exc}"
                )

            s2.sendall(b"Y")
            r, _, _ = select.select([tty_fd], [], [], 1.0)
            assert r, "TTY not readable during second session (reconnect)"
            data = os.read(tty_fd, 256)
            assert b"Y" in data, \
                f"Expected b'Y' in second-session payload, got {data!r}"

            s2.close()

            assert daemon_proc.poll() is None, \
                "Daemon exited unexpectedly after second client disconnect"

        finally:
            restore_tty(tty_fd, saved)
