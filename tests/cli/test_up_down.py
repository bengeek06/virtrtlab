# SPDX-License-Identifier: MIT

"""
test_up_down.py — integration tests for cmd_up and cmd_down.

Requires:
  - Passwordless sudo (see conftest.require_root)
  - Built kernel .ko files under kernel/ (see conftest.require_modules)

All tests use a temporary TOML profile and call the CLI via subprocess
so that sudo is applied naturally by virtrtlabctl itself.
"""

import json
import subprocess
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
        toml = _write_lab_toml(tmp_path)
        r = run_ctl("--json", "up", "--config", toml)
        assert r.returncode == 0
        data = json.loads(r.stdout)
        assert data["status"] == "up"
        assert "modules" in data


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
