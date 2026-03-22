# SPDX-License-Identifier: MIT

"""
conftest.py — shared fixtures for virtrtlabctl CLI tests.

Pure Python tests (test_profile.py, test_sysfs.py) monkey-patch
ctl.SYSFS_ROOT / ctl.RUN_DIR via the fake_sysfs fixture and never
touch real sysfs or load kernel modules.

Integration tests (test_up_down.py, test_daemon.py) require:
    - Cached sudo credentials (`sudo -v` in the current session)
  - Built .ko files under kernel/
  - virtrtlabd binary under daemon/
They are automatically skipped when those prerequisites are absent.
"""

import os
import subprocess
import sys

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

CLI_DIR = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", "..", "cli"))
KERNEL_DIR = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", "..", "kernel"))
DAEMON_BIN = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", "..", "daemon", "virtrtlabd"))
CLI_SCRIPT = os.path.join(CLI_DIR, "virtrtlabctl.py")

KO = {
    "core": os.path.join(KERNEL_DIR, "virtrtlab_core.ko"),
    "uart": os.path.join(KERNEL_DIR, "virtrtlab_uart.ko"),
    "gpio": os.path.join(KERNEL_DIR, "virtrtlab_gpio.ko"),
}

# ---------------------------------------------------------------------------
# Import CLI module for unit tests
# ---------------------------------------------------------------------------

sys.path.insert(0, CLI_DIR)
import virtrtlabctl as ctl  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_args(*argv: str):
    """Parse argv through virtrtlabctl's argparse and return a Namespace."""
    return ctl._build_parser().parse_args(list(argv))


def run_ctl(*argv: str, sudo: bool = False) -> subprocess.CompletedProcess:
    """Run the virtrtlabctl CLI in a subprocess."""
    prefix = ["sudo"] if sudo else []
    return subprocess.run(
        prefix + ["python3", CLI_SCRIPT] + list(argv),
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# Fixtures — unit / fake-sysfs tests
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_sysfs(tmp_path, monkeypatch):
    """
    Create a temporary sysfs + run directory tree and redirect ctl constants.

    Yields a dict: {'sysfs': Path, 'run': Path}
    """
    sysfs = tmp_path / "sys"
    run = tmp_path / "run"

    # Minimal sysfs skeleton
    (sysfs / "buses" / "vrtlbus0").mkdir(parents=True)
    (sysfs / "buses" / "vrtlbus0" / "state").write_text("up\n")
    (sysfs / "buses" / "vrtlbus0" / "seed").write_text("0\n")
    (sysfs / "devices").mkdir()
    run.mkdir()

    monkeypatch.setattr(ctl, "SYSFS_ROOT", str(sysfs))
    monkeypatch.setattr(ctl, "RUN_DIR", str(run))

    yield {"sysfs": sysfs, "run": run}


@pytest.fixture
def fake_uart(fake_sysfs):
    """Add a uart0 device subtree to the fake sysfs."""
    dev = fake_sysfs["sysfs"] / "devices" / "uart0"
    stats = dev / "stats"
    stats.mkdir(parents=True)
    (dev / "type").write_text("uart\n")
    (dev / "bus").write_text("vrtlbus0\n")
    (dev / "enabled").write_text("1\n")
    (dev / "baud").write_text("115200\n")
    (dev / "latency_ns").write_text("0\n")
    (stats / "tx_bytes").write_text("1048576\n")
    (stats / "rx_bytes").write_text("4096\n")
    (stats / "drops").write_text("3\n")
    (stats / "reset").write_text("0\n")
    return fake_sysfs


# ---------------------------------------------------------------------------
# Fixtures — integration tests
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def require_root():
    """Skip integration tests if cached sudo credentials are not available."""
    r = subprocess.run(["sudo", "-n", "true"], capture_output=True)
    if r.returncode != 0:
        pytest.skip(
            "Integration tests require cached sudo credentials (run: sudo -v first)"
        )


@pytest.fixture(scope="session")
def require_modules(require_root):
    """Skip if kernel .ko files haven't been built."""
    for name, path in KO.items():
        if not os.path.exists(path):
            pytest.skip(f"Module not built: {path} — run 'make' in kernel/")


@pytest.fixture(scope="session")
def require_daemon_bin(require_root):
    """Skip if virtrtlabd binary is absent."""
    if not os.path.exists(DAEMON_BIN):
        pytest.skip(f"Daemon binary not found: {DAEMON_BIN}")
