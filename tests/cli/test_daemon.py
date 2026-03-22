# SPDX-License-Identifier: MIT

"""
test_daemon.py — integration tests for cmd_daemon.

Requires:
    - Cached sudo credentials (see conftest.require_root; run sudo -v first)
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
from types import SimpleNamespace

import pytest

from conftest import CLI_SCRIPT, KERNEL_DIR, ctl, make_args, run_ctl


# ---------------------------------------------------------------------------
# Pure tests — no kernel needed
# ---------------------------------------------------------------------------

class TestDaemonStatusStopped:
    """daemon status without a running daemon — pure Python, no root."""

    def test_daemon_status_stopped_exit3(self, monkeypatch):
        monkeypatch.setattr(ctl, "_daemon_pid", lambda run_dir=ctl.RUN_DIR: None)
        args = make_args("daemon", "status")
        rc = ctl.cmd_daemon(args)
        assert rc == 3

    def test_daemon_status_stopped_json(self, capsys, monkeypatch):
        monkeypatch.setattr(ctl, "_daemon_pid", lambda run_dir=ctl.RUN_DIR: None)
        args = make_args("--json", "daemon", "status")
        rc = ctl.cmd_daemon(args)
        assert rc == 3
        data = json.loads(capsys.readouterr().out)
        assert data["state"] == "stopped"
        assert data["pid"] is None

    def test_daemon_status_stopped_human(self, capsys, monkeypatch):
        monkeypatch.setattr(ctl, "_daemon_pid", lambda run_dir=ctl.RUN_DIR: None)
        args = make_args("daemon", "status")
        rc = ctl.cmd_daemon(args)
        assert rc == 3
        out = capsys.readouterr().out
        assert "stopped" in out


class TestDaemonLaunchDetachment:
    """Daemon launch paths preserve sudo interactivity and detach afterwards."""

    def test_launch_daemon_uses_sudo_helper_for_non_root(self, monkeypatch):
        run_cmd_calls = []

        def fake_run_cmd(cmd, **kwargs):
            run_cmd_calls.append((cmd, kwargs))
            return None

        monkeypatch.setattr(ctl.os, "geteuid", lambda: 1000)
        monkeypatch.setattr(ctl, "_run_cmd", fake_run_cmd)
        monkeypatch.setattr(ctl, "_write_run_file", lambda *args, **kwargs: None)

        pid = ctl._launch_daemon(False, 2, "/tmp/virtrtlab-test")

        assert pid is None
        assert len(run_cmd_calls) == 1
        cmd, kwargs = run_cmd_calls[0]
        assert cmd == [
            "sudo",
            ctl.sys.executable,
            str(ctl._SCRIPT_PATH),
            "__spawn-daemon-detached",
            "--num-uarts",
            "2",
            "--run-dir",
            "/tmp/virtrtlab-test",
            "--pid-file",
            "/tmp/virtrtlab-test/daemon.pid",
            "--daemon-bin",
            ctl.DAEMON_BIN,
        ]
        assert kwargs["exit_code"] == 1

    def test_launch_daemon_detaches_direct_path(self, monkeypatch):
        popen_calls = []
        write_calls = []

        def fake_popen(*args, **kwargs):
            popen_calls.append((args, kwargs))
            return SimpleNamespace(pid=1111)

        monkeypatch.setattr(ctl.os, "geteuid", lambda: 0)
        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        monkeypatch.setattr(
            ctl,
            "_write_run_file",
            lambda *args, **kwargs: write_calls.append((args, kwargs)),
        )

        pid = ctl._launch_daemon(False, 2, "/tmp/virtrtlab-test")

        assert pid == 1111
        assert len(popen_calls) == 1
        cmd_args, kwargs = popen_calls[0]
        assert cmd_args[0] == [
            ctl.DAEMON_BIN,
            "--num-uarts",
            "2",
            "--run-dir",
            "/tmp/virtrtlab-test",
        ]
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs["stdout"] is subprocess.DEVNULL
        assert kwargs["stderr"] is subprocess.DEVNULL
        assert kwargs["start_new_session"] is True
        assert kwargs["close_fds"] is True
        assert write_calls == [
            (("daemon.pid", "1111\n", False), {"run_dir": "/tmp/virtrtlab-test"})
        ]

    def test_internal_spawn_command_writes_pid_file(self, monkeypatch, tmp_path):
        popen_calls = []

        def fake_popen(*args, **kwargs):
            popen_calls.append((args, kwargs))
            return SimpleNamespace(pid=2222)

        monkeypatch.setattr(subprocess, "Popen", fake_popen)

        pid_file = tmp_path / "daemon.pid"
        args = make_args(
            "__spawn-daemon-detached",
            "--num-uarts",
            "1",
            "--run-dir",
            "/tmp/virtrtlab-test",
            "--pid-file",
            str(pid_file),
            "--daemon-bin",
            "/tmp/virtrtlabd",
        )
        rc = ctl.cmd_spawn_daemon_detached(args)

        assert rc == 0
        assert len(popen_calls) == 1
        cmd_args, kwargs = popen_calls[0]
        assert cmd_args[0] == [
            "/tmp/virtrtlabd",
            "--num-uarts",
            "1",
            "--run-dir",
            "/tmp/virtrtlab-test",
        ]
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs["stdout"] is subprocess.DEVNULL
        assert kwargs["stderr"] is subprocess.DEVNULL
        assert kwargs["start_new_session"] is True
        assert kwargs["close_fds"] is True
        assert pid_file.read_text() == "2222\n"

    def test_internal_spawn_command_wraps_popen_error(self, monkeypatch, tmp_path):
        def fake_popen(*args, **kwargs):
            raise OSError("boom")

        monkeypatch.setattr(subprocess, "Popen", fake_popen)

        pid_file = tmp_path / "daemon.pid"
        args = make_args(
            "__spawn-daemon-detached",
            "--num-uarts",
            "1",
            "--run-dir",
            "/tmp/virtrtlab-test",
            "--pid-file",
            str(pid_file),
            "--daemon-bin",
            "/tmp/virtrtlabd",
        )

        with pytest.raises(ctl.VirtrtlabError, match="failed to start daemon"):
            ctl.cmd_spawn_daemon_detached(args)

    def test_launch_daemon_wraps_direct_spawn_error(self, monkeypatch):
        monkeypatch.setattr(ctl.os, "geteuid", lambda: 0)

        def fake_popen(*args, **kwargs):
            raise FileNotFoundError("missing")

        monkeypatch.setattr(subprocess, "Popen", fake_popen)

        with pytest.raises(ctl.VirtrtlabError, match="failed to start daemon"):
            ctl._launch_daemon(False, 2, "/tmp/virtrtlab-test")

    def test_internal_spawn_command_reports_pid_write_error(self, monkeypatch, tmp_path):
        def fake_popen(*args, **kwargs):
            return SimpleNamespace(pid=2222)

        def fake_write_text(self, data, encoding=None, errors=None, newline=None):
            raise OSError("read-only")

        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        monkeypatch.setattr(Path, "write_text", fake_write_text)

        pid_file = tmp_path / "daemon.pid"
        args = make_args(
            "__spawn-daemon-detached",
            "--num-uarts",
            "1",
            "--run-dir",
            "/tmp/virtrtlab-test",
            "--pid-file",
            str(pid_file),
            "--daemon-bin",
            "/tmp/virtrtlabd",
        )

        with pytest.raises(ctl.VirtrtlabError, match="failed to write daemon pid file"):
            ctl.cmd_spawn_daemon_detached(args)


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
