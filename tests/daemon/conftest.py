"""
conftest.py — shared fixtures for VirtRTLab daemon integration tests.

These tests require:
  - passwordless sudo
  - virtrtlab_core.ko and virtrtlab_uart.ko built and loadable
  - daemon/virtrtlabd built (cd daemon && make)

The module-loading helpers are imported from tests/kernel/conftest.py so
that daemon tests reuse the same module lifecycle without duplication.
"""

import os
import subprocess
import termios
import time
import tty

import pytest

# ---------------------------------------------------------------------------
# Re-use kernel helpers from tests/kernel/conftest.py
# ---------------------------------------------------------------------------

_KERNEL_TEST_DIR = os.path.realpath(
    os.path.join(os.path.dirname(__file__), "..", "kernel")
)

# Load the kernel conftest under an explicit module name to avoid the circular
# import that arises when Python resolves a bare `from conftest import …`
# against the daemon conftest currently being initialised.
import importlib.util as _ilu

_spec = _ilu.spec_from_file_location(
    "kernel_conftest",
    os.path.join(_KERNEL_TEST_DIR, "conftest.py"),
)
_kc = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_kc)

KO            = _kc.KO
_run          = _kc._run
_insmod       = _kc._insmod
_rmmod        = _kc._rmmod
_module_loaded = _kc._module_loaded
dmesg_lines   = _kc.dmesg_lines

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

DAEMON_DIR = os.path.realpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "daemon")
)
DAEMON_BIN = os.path.join(DAEMON_DIR, "virtrtlabd")
RUN_DIR    = "/run/virtrtlab"
SOCK_PATH  = f"{RUN_DIR}/uart0.sock"
TTY_PATH   = "/dev/ttyVIRTLAB0"

# ---------------------------------------------------------------------------
# TTY helpers (shared by relay and reconnect tests)
# ---------------------------------------------------------------------------

def open_tty_raw():
    """
    Open TTY_PATH in raw mode (no line buffering, no echo, no CR/LF mapping).

    Returns (fd, saved_attrs).  Caller must call restore_tty(fd, saved_attrs)
    in a finally block.  Requires root or membership in the dialout group.
    """
    fd = os.open(TTY_PATH, os.O_RDWR | os.O_NOCTTY | os.O_CLOEXEC)
    saved = termios.tcgetattr(fd)
    tty.setraw(fd)
    return fd, saved


def restore_tty(fd, saved_attrs):
    """Restore terminal settings and close the fd."""
    try:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved_attrs)
    finally:
        os.close(fd)

# ---------------------------------------------------------------------------
# Session-level guard
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def check_root():
    """Abort the session early if passwordless sudo is not available."""
    r = subprocess.run(["sudo", "-n", "true"], capture_output=True)
    if r.returncode != 0:
        pytest.skip("Tests require passwordless sudo — run: sudo -v first")

# ---------------------------------------------------------------------------
# Module fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def core_module():
    """Load virtrtlab_core before the test, unload after."""
    if not os.path.exists(KO["core"]):
        pytest.skip(f"Module not built: {KO['core']}")
    _rmmod("virtrtlab_core")
    _insmod(KO["core"])
    assert _module_loaded("virtrtlab_core"), "virtrtlab_core failed to load"
    yield
    _rmmod("virtrtlab_core")
    assert not _module_loaded("virtrtlab_core"), "virtrtlab_core failed to unload"


@pytest.fixture()
def uart_module(core_module):
    """Load virtrtlab_uart (depends on core_module)."""
    if not os.path.exists(KO["uart"]):
        pytest.skip(f"Module not built: {KO['uart']}")
    _rmmod("virtrtlab_uart")
    _insmod(KO["uart"])
    assert _module_loaded("virtrtlab_uart"), "virtrtlab_uart failed to load"
    yield
    _rmmod("virtrtlab_uart")
    assert not _module_loaded("virtrtlab_uart"), "virtrtlab_uart failed to unload"

# ---------------------------------------------------------------------------
# Daemon fixture
# ---------------------------------------------------------------------------

@pytest.fixture()
def daemon_proc(uart_module):
    """
    Start virtrtlabd for one UART instance, wait up to 2 s for the AF_UNIX
    socket to appear, yield the Popen object, then teardown via SIGTERM.

    Teardown asserts returncode == 0 (AC4 — clean shutdown).
    Depends on uart_module so /dev/virtrtlab-wire0 exists before the daemon
    tries to open it.
    """
    if not os.path.exists(DAEMON_BIN):
        pytest.skip(f"Daemon not built: {DAEMON_BIN}")

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

    yield proc

    _run(["kill", "-TERM", str(proc.pid)], check=False)
    try:
        ret = proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        pytest.fail("virtrtlabd did not exit within 3 s after SIGTERM")

    assert ret == 0, f"virtrtlabd exited with code {ret} (expected 0)"
