"""
test_daemon_startup.py — daemon startup tests for virtrtlabd (issue #10).

Acceptance criteria covered:
  AC1 — daemon starts and creates /run/virtrtlab/uart0.sock
  AC5 — startup fails cleanly when the wire device is missing
"""

import os
import stat
import subprocess
import time

import pytest

from conftest import DAEMON_BIN, RUN_DIR


SOCK_PATH = f"{RUN_DIR}/uart0.sock"


# ---------------------------------------------------------------------------
# AC1 — socket is created at startup
# ---------------------------------------------------------------------------

class TestDaemonStartup:
    """virtrtlabd creates the AF_UNIX socket at startup."""

    def test_socket_created(self, daemon_proc):
        """Socket file exists once the daemon is running."""
        assert os.path.exists(SOCK_PATH), \
            f"{SOCK_PATH} not found after daemon start"

    def test_socket_is_unix_stream(self, daemon_proc):
        """The created file is an AF_UNIX socket (not a regular file)."""
        mode = os.stat(SOCK_PATH).st_mode
        assert stat.S_ISSOCK(mode), \
            f"{SOCK_PATH} exists but is not a socket (mode={oct(mode)})"


# ---------------------------------------------------------------------------
# AC5 — startup fails cleanly when the wire device is missing
# ---------------------------------------------------------------------------

class TestDaemonMissingWire:
    """virtrtlabd exits with code 1 when /dev/virtrtlab-wireN is absent."""

    def test_missing_wire_device_exits_nonzero(self, core_module):
        """
        With only virtrtlab_core loaded (no virtrtlab_uart), no wire device
        exists. The daemon must detect this, print a diagnostic, and exit
        with a non-zero code — it must NOT hang or create a partial socket.
        """
        if not os.path.exists(DAEMON_BIN):
            pytest.skip(f"Daemon not built: {DAEMON_BIN}")

        # Ensure no stale socket from a previous run interferes.
        stale = f"{RUN_DIR}/uart0.sock"
        subprocess.run(["sudo", "rm", "-f", stale], check=False)

        proc = subprocess.Popen(
            ["sudo", DAEMON_BIN, "--num-uarts", "1", "--run-dir", RUN_DIR],
            stderr=subprocess.PIPE,
            text=True,
        )

        try:
            ret = proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            pytest.fail("virtrtlabd hung instead of exiting on missing wire device")

        assert ret != 0, \
            "virtrtlabd should exit non-zero when wire device is missing"

        # Socket must NOT have been created.
        assert not os.path.exists(stale), \
            f"{stale} must not exist when startup fails"
