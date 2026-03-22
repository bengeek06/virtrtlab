# SPDX-License-Identifier: MIT

"""
test_up_down.py — integration tests for cmd_up and cmd_down.

Requires:
    - Cached sudo credentials (see conftest.require_root; run sudo -v first)
  - Built kernel .ko files under kernel/ (see conftest.require_modules)

All tests use a temporary TOML profile and call the CLI via subprocess
so that sudo is applied naturally by virtrtlabctl itself.
"""

import json
import os
import pty
import select
import subprocess
import termios
import time
from pathlib import Path

import pytest

from conftest import CLI_SCRIPT, KO, KERNEL_DIR, run_ctl


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _module_loaded(name: str) -> bool:
    r = subprocess.run(
        ["grep", "-q", f"^{name} ", "/proc/modules"],
        capture_output=True,
    )
    return r.returncode == 0


def _write_lab_toml(path: Path, uart_count: int = 1) -> str:
    toml = (
        f'[build]\nmodule_dir = "{KERNEL_DIR}"\n\n'
        f"[[devices]]\ntype = \"uart\"\ncount = {uart_count}\n"
    )
    p = path / "lab.toml"
    p.write_text(toml)
    return str(p)


def _run_cli_pty(
    *argv: str, sudo: bool = False, timeout: float | None = None
) -> tuple[int, str, list[object], list[object]]:
    """Run the CLI on a pseudo-terminal and return rc, output, before, after."""
    if timeout is None:
        timeout = float(os.environ.get("VIRTRTLAB_TEST_PTY_TIMEOUT", "15.0"))

    master_fd, slave_fd = pty.openpty()
    before = termios.tcgetattr(slave_fd)
    proc = subprocess.Popen(
        (["sudo"] if sudo else []) + ["python3", CLI_SCRIPT, *argv],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        close_fds=True,
    )

    output = bytearray()
    deadline = time.monotonic() + timeout
    idle_after_exit = 0
    os.set_blocking(master_fd, False)

    try:
        while True:
            if time.monotonic() >= deadline:
                proc.kill()
                raise AssertionError(f"CLI hung on PTY: {' '.join(argv)}")

            ready, _, _ = select.select([master_fd], [], [], 0.1)
            read_any = False
            if ready:
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError:
                    chunk = b""
                if chunk:
                    output.extend(chunk)
                    read_any = True

            if proc.poll() is None:
                idle_after_exit = 0
                continue

            if read_any:
                idle_after_exit = 0
                continue

            idle_after_exit += 1
            if idle_after_exit >= 3:
                break

        after = termios.tcgetattr(slave_fd)
        return proc.wait(timeout=1.0), output.decode(errors="replace"), before, after
    finally:
        os.close(master_fd)
        os.close(slave_fd)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestCmdUp:
    """cmd_up integration — module loading and socket readiness."""

    @pytest.fixture(autouse=True)
    def _cleanup(self, require_modules):
        """Always tear down after each test."""
        yield
        run_ctl("down")

    def test_up_loads_core_and_uart(self, tmp_path):
        toml = _write_lab_toml(tmp_path)
        r = run_ctl("up", "--config", toml)
        assert r.returncode == 0, f"up failed:\n{r.stderr}"
        assert _module_loaded("virtrtlab_core")
        assert _module_loaded("virtrtlab_uart")

    def test_up_creates_modules_list(self, tmp_path):
        toml = _write_lab_toml(tmp_path)
        run_ctl("up", "--config", toml)
        modules_list = Path("/run/virtrtlab/modules.list")
        assert modules_list.exists(), "modules.list not written by up"
        content = modules_list.read_text()
        assert "virtrtlab_core" in content
        assert "virtrtlab_uart" in content

    def test_up_creates_daemon_socket(self, tmp_path):
        toml = _write_lab_toml(tmp_path, uart_count=1)
        r = run_ctl("up", "--config", toml)
        assert r.returncode == 0
        assert Path("/run/virtrtlab/uart0.sock").exists()

    def test_up_idempotent(self, tmp_path):
        """Calling up twice should warn and exit 0, not fail."""
        toml = _write_lab_toml(tmp_path)
        run_ctl("up", "--config", toml)
        r = run_ctl("up", "--config", toml)
        assert r.returncode == 0
        assert "already up" in r.stderr.lower() or r.returncode == 0

    def test_up_json_output(self, tmp_path):
        """cmd_up --json emits the AUT integration contract (device-contract.md).

        The top-level key is 'devices', each entry has 'name', 'type', and 'env'.
        There is no 'status' key — status is represented by exit code 0.
        """
        toml = _write_lab_toml(tmp_path)
        r = run_ctl("--json", "up", "--config", toml)
        assert r.returncode == 0
        data = json.loads(r.stdout)
        assert "devices" in data, f"expected 'devices' key, got: {list(data.keys())}"
        assert len(data["devices"]) >= 1
        device = data["devices"][0]
        assert device["type"] == "uart"
        assert device["name"] == "uart0"
        assert "aut_path" in device
        assert "env" in device
        assert "VIRTRTLAB_UART0" in device["env"]

    def test_up_preserves_terminal_with_internal_sudo(self, require_daemon_bin, require_modules, tmp_path):
        toml = _write_lab_toml(tmp_path)
        try:
            rc, output, before, after = _run_cli_pty("up", "--config", toml)
            assert rc == 0, output
            assert before == after
            assert "[ok] uart0 loaded" in output
        finally:
            run_ctl("down")

    def test_up_preserves_terminal_when_already_root(self, require_daemon_bin, require_modules, tmp_path):
        toml = _write_lab_toml(tmp_path)
        try:
            rc, output, before, after = _run_cli_pty("--no-sudo", "up", "--config", toml, sudo=True)
            assert rc == 0, output
            assert before == after
            assert "[ok] uart0 loaded" in output
        finally:
            run_ctl("down", sudo=True)


class TestCmdDown:
    """cmd_down integration — module unloading."""

    def test_down_unloads_modules(self, require_modules, tmp_path):
        toml = _write_lab_toml(tmp_path)
        run_ctl("up", "--config", toml)
        assert _module_loaded("virtrtlab_uart"), "uart not loaded before down"
        r = run_ctl("down")
        assert r.returncode == 0, f"down failed:\n{r.stderr}"
        time.sleep(0.5)
        assert not _module_loaded("virtrtlab_uart"), "uart still loaded after down"
        assert not _module_loaded("virtrtlab_core"), "core still loaded after down"

    def test_down_without_modules_list_exits_0(self, require_modules):
        """down warns and exits 0 even if modules.list is missing."""
        # Ensure modules.list doesn't exist
        mlist = Path("/run/virtrtlab/modules.list")
        if mlist.exists():
            subprocess.run(["sudo", "rm", "-f", str(mlist)], check=False)

        r = run_ctl("down")
        assert r.returncode == 0
        assert "warning" in r.stderr.lower()

    def test_down_removes_modules_list(self, require_modules, tmp_path):
        toml = _write_lab_toml(tmp_path)
        run_ctl("up", "--config", toml)
        mlist = Path("/run/virtrtlab/modules.list")
        assert mlist.exists()
        run_ctl("down")
        assert not mlist.exists()
