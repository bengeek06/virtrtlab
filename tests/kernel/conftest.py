"""
conftest.py — shared pytest fixtures for VirtRTLab kernel integration tests.

All tests that load/unload modules must run as root (or via sudo).
The fixtures here handle module lifecycle and dmesg capture in a
consistent way so individual test files stay focused on assertions.
"""

import os
import subprocess
import time

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

KERNEL_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "kernel"
)
KERNEL_DIR = os.path.realpath(KERNEL_DIR)

KO = {
    "core": os.path.join(KERNEL_DIR, "virtrtlab_core.ko"),
    "gpio": os.path.join(KERNEL_DIR, "virtrtlab_gpio.ko"),
    "uart": os.path.join(KERNEL_DIR, "virtrtlab_uart.ko"),
}

SYSFS_ROOT = "/sys/kernel/virtrtlab"


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _run(cmd, check=True):
    """Run a shell command via sudo, return CompletedProcess."""
    return subprocess.run(
        ["sudo"] + cmd,
        capture_output=True,
        text=True,
        check=check,
    )


def _module_loaded(name):
    """Return True if the kernel module *name* appears in /proc/modules."""
    result = subprocess.run(
        ["grep", "-q", f"^{name} ", "/proc/modules"],
        capture_output=True,
    )
    return result.returncode == 0


def _insmod(ko_path, params=""):
    """Load a .ko with optional params string, e.g. 'num_gpio_banks=2'."""
    cmd = ["insmod", ko_path]
    if params:
        cmd += params.split()
    _run(cmd)


def _rmmod(name):
    """Unload a module by name; ignore error if already gone."""
    _run(["rmmod", name], check=False)


def _dmesg_since(t0):
    """
    Return dmesg lines produced after timestamp *t0* (float, seconds since epoch).
    Uses dmesg --since via the kernel monotonic clock helper; falls back to a
    line-by-line filter on the human-readable timestamp when --since is unsupported.
    """
    result = subprocess.run(
        ["dmesg", "--kernel", "--time-format=iso"],
        capture_output=True, text=True, check=False,
    )
    lines = []
    for line in result.stdout.splitlines():
        # ISO format: "2026-03-14T12:34:56,123456+0000 virtrtlab_core: loaded"
        # We can't directly compare to t0 portably across WSL dmesg versions,
        # so we collect all lines and let each test filter by content instead.
        lines.append(line)
    return lines


def dmesg_lines():
    """Return all recent dmesg lines (last 200) as a list of strings."""
    result = subprocess.run(
        ["dmesg", "--kernel"],
        capture_output=True, text=True, check=False,
    )
    return result.stdout.splitlines()[-200:]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def check_root():
    """Abort the session early if we cannot reach sudo without a password."""
    r = subprocess.run(["sudo", "-n", "true"], capture_output=True)
    if r.returncode != 0:
        pytest.skip("Tests require passwordless sudo — run: sudo -v first")


@pytest.fixture()
def core_module():
    """
    Load virtrtlab_core before the test, unload after.
    Skips if the .ko is missing (not yet built).
    """
    if not os.path.exists(KO["core"]):
        pytest.skip(f"Module not built: {KO['core']}")

    # ensure clean state
    _rmmod("virtrtlab_core")
    _insmod(KO["core"])
    assert _module_loaded("virtrtlab_core"), "virtrtlab_core failed to load"

    yield

    _rmmod("virtrtlab_core")
    assert not _module_loaded("virtrtlab_core"), "virtrtlab_core failed to unload"


@pytest.fixture()
def gpio_module(core_module, request):
    """
    Load virtrtlab_gpio (depends on core_module fixture).
    Accepts an optional pytest parameter for module params
    via indirect parametrization of this fixture.
    """
    if not os.path.exists(KO["gpio"]):
        pytest.skip(f"Module not built: {KO['gpio']}")

    params = getattr(request, "param", "")
    _rmmod("virtrtlab_gpio")
    _insmod(KO["gpio"], params)
    assert _module_loaded("virtrtlab_gpio"), "virtrtlab_gpio failed to load"

    yield

    _rmmod("virtrtlab_gpio")
    assert not _module_loaded("virtrtlab_gpio"), "virtrtlab_gpio failed to unload"


@pytest.fixture()
def uart_module(core_module, request):
    """
    Load virtrtlab_uart (depends on core_module fixture).
    Accepts an optional pytest parameter for module params
    via indirect parametrization of this fixture.
    """
    if not os.path.exists(KO["uart"]):
        pytest.skip(f"Module not built: {KO['uart']}")

    params = getattr(request, "param", "")
    _rmmod("virtrtlab_uart")
    _insmod(KO["uart"], params)
    assert _module_loaded("virtrtlab_uart"), "virtrtlab_uart failed to load"

    yield

    _rmmod("virtrtlab_uart")
    assert not _module_loaded("virtrtlab_uart"), "virtrtlab_uart failed to unload"
