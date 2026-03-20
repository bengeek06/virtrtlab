# SPDX-License-Identifier: MIT

"""
test_daemon.py — integration tests for cmd_daemon.

Requires:
  - Passwordless sudo (see conftest.require_root)
  - virtrtlabd binary under daemon/ (see conftest.require_daemon_bin)
  - virtrtlab_core and virtrtlab_uart kernel modules loaded (brought up
    by the up_lab fixture which calls virtrtlabctl up).

Tests cover: daemon status when stopped (pure Python, no root),
daemon start / stop via the CLI subprocess.
"""

import json
import subprocess
import time
from pathlib import Path

import pytest

from conftest import CLI_SCRIPT, KERNEL_DIR, ctl, make_args, run_ctl


# ---------------------------------------------------------------------------
# Pure tests — no kernel needed
# ---------------------------------------------------------------------------

class TestDaemonStatusStopped:
    """daemon status without a running daemon — pure Python, no root."""

    def test_daemon_status_stopped_exit3(self):
        args = make_args("daemon", "status")
        rc = ctl.cmd_daemon(args)
        assert rc == 3

    def test_daemon_status_stopped_json(self, capsys):
        args = make_args("--json", "daemon", "status")
        rc = ctl.cmd_daemon(args)
        assert rc == 3
        data = json.loads(capsys.readouterr().out)
        assert data["state"] == "stopped"
        assert data["pid"] is None

    def test_daemon_status_stopped_human(self, capsys):
        args = make_args("daemon", "status")
        ctl.cmd_daemon(args)
        out = capsys.readouterr().out
        assert "stopped" in out


# ---------------------------------------------------------------------------
# Integration tests — require daemon binary + kernel modules
# ---------------------------------------------------------------------------

def _write_lab_toml(path: Path, uart_count: int = 1) -> str:
    toml = (
        f'[build]\nmodule_dir = "{KERNEL_DIR}"\n\n'
        f"[[devices]]\ntype = \"uart\"\ncount = {uart_count}\n"
    )
    p = path / "lab.toml"
    p.write_text(toml)
    return str(p)


class TestDaemonStartStop:
    """daemon start / stop / status via CLI subprocess."""

    @pytest.fixture(autouse=True)
    def _bring_up_lab(self, require_daemon_bin, require_modules, tmp_path):
        """Load modules and ensure a clean state before/after each test."""
        toml = _write_lab_toml(tmp_path)
        run_ctl("up", "--config", toml)
        yield
        run_ctl("down")

    def test_daemon_status_running(self):
        r = run_ctl("daemon", "status")
        assert r.returncode == 0
        assert "running" in r.stdout

    def test_daemon_status_running_json(self):
        r = run_ctl("--json", "daemon", "status")
        assert r.returncode == 0
        data = json.loads(r.stdout)
        assert data["state"] == "running"
        assert isinstance(data["pid"], int)

    def test_daemon_stop(self):
        # Stop the daemon (up has started it)
        r = run_ctl("daemon", "stop")
        assert r.returncode == 0
        time.sleep(0.5)
        # Daemon should now be gone
        r2 = run_ctl("daemon", "status")
        assert r2.returncode == 3

    def test_daemon_start_when_already_running_exit3(self):
        # Daemon is already running (started by up fixture)
        r = run_ctl("daemon", "start", "--num-uarts", "1")
        assert r.returncode == 3

    def test_daemon_start_after_stop(self, tmp_path):
        # Stop, then restart independently
        run_ctl("daemon", "stop")
        time.sleep(0.5)

        r = run_ctl("daemon", "start", "--num-uarts", "1")
        assert r.returncode == 0
        assert Path("/run/virtrtlab/uart0.sock").exists()
