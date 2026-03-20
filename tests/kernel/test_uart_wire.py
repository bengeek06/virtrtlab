# SPDX-License-Identifier: MIT

"""
test_uart_wire.py — data-plane integration tests for virtrtlab_uart wire device.

Acceptance criteria (issue #2):
  AC1  — /dev/ttyVIRTLABx and /dev/virtrtlab-wireN are created on insmod
  AC2  — /dev/virtrtlab-wireN refuses a second open with EBUSY
  AC3  — AUT writes → simulator reads (ttyVIRTLABx write → wire read)
  AC4  — simulator writes → AUT reads (wire write → ttyVIRTLABx read)
  AC5  — tx_buf_sz / rx_buf_sz refuse resize while TTY or wire is open (EBUSY)
  AC6  — bus reset: wire_read() returns 0 (EOF); fault attrs and stats are cleared
"""

import errno
import os
import select
import stat
import termios
import time
import tty

import pytest

from conftest import (
    KO,
    SYSFS_ROOT,
    _insmod,
    _module_loaded,
    _rmmod,
)

DEVICES_ROOT = f"{SYSFS_ROOT}/devices"
BUS0_STATE   = f"{SYSFS_ROOT}/buses/vrtlbus0/state"

TTY_DEV      = "/dev/ttyVIRTLAB0"
WIRE_DEV     = "/dev/virtrtlab-wire0"

READ_TIMEOUT = 2.0  # seconds


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _uart_path(idx, attr=""):
    base = f"{DEVICES_ROOT}/uart{idx}"
    return f"{base}/{attr}" if attr else base


def _read_sysfs(path):
    with open(path) as f:
        return f.read().strip()


def _write_sysfs(path, value):
    """Write *value* to sysfs *path* as root via /proc/self/fd trick."""
    with open(path, "w") as f:
        f.write(str(value))


def _open_raw_tty(path):
    """Open a TTY in raw mode.  Returns fd."""
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    tty.setraw(fd, termios.TCSANOW)
    return fd


def _read_with_timeout(fd, nbytes, timeout=READ_TIMEOUT):
    """Read up to *nbytes* with *timeout* seconds.  Returns bytes."""
    buf = b""
    deadline = time.monotonic() + timeout
    while len(buf) < nbytes:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        r, _, _ = select.select([fd], [], [], remaining)
        if not r:
            break
        chunk = os.read(fd, nbytes - len(buf))
        if not chunk:   # EOF
            break
        buf += chunk
    return buf


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_bus_after(uart_module):
    """Ensure the bus is UP and stats are clean before each test."""
    # Reset to clear any previous state
    try:
        _write_sysfs(BUS0_STATE, "reset")
    except OSError:
        pass
    yield
    # Restore default buf sizes in case a test changed them
    for attr in ("tx_buf_sz", "rx_buf_sz"):
        try:
            _write_sysfs(_uart_path(0, attr), 4096)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# AC1 — Device nodes
# ---------------------------------------------------------------------------

class TestDeviceNodes:
    """AC1 — device nodes created on insmod."""

    def test_tty_exists(self, uart_module):
        assert os.path.exists(TTY_DEV), f"{TTY_DEV} not found after insmod"

    def test_wire_exists(self, uart_module):
        assert os.path.exists(WIRE_DEV), f"{WIRE_DEV} not found after insmod"

    def test_tty_is_char_device(self, uart_module):
        assert stat.S_ISCHR(os.stat(TTY_DEV).st_mode)

    def test_wire_is_char_device(self, uart_module):
        assert stat.S_ISCHR(os.stat(WIRE_DEV).st_mode)


# ---------------------------------------------------------------------------
# AC2 — Wire exclusive open
# ---------------------------------------------------------------------------

class TestWireExclusive:
    """AC2 — wire device refuses a second open with EBUSY."""

    def test_second_open_ebusy(self, uart_module):
        fd = os.open(WIRE_DEV, os.O_RDONLY | os.O_NONBLOCK)
        try:
            with pytest.raises(OSError) as exc:
                os.open(WIRE_DEV, os.O_RDONLY | os.O_NONBLOCK)
            assert exc.value.errno == errno.EBUSY
        finally:
            os.close(fd)

    def test_reopen_after_close(self, uart_module):
        """After the exclusive fd is closed the device can be reopened."""
        fd = os.open(WIRE_DEV, os.O_RDONLY | os.O_NONBLOCK)
        os.close(fd)
        fd = os.open(WIRE_DEV, os.O_RDONLY | os.O_NONBLOCK)
        os.close(fd)


# ---------------------------------------------------------------------------
# AC3 — AUT → wire
# ---------------------------------------------------------------------------

class TestAUTtoWire:
    """AC3 — data written by AUT appears on the wire side."""

    def test_aut_write_wire_read(self, uart_module):
        # Open TTY first so tty_port_activate() allocates kfifos.
        tty_fd  = _open_raw_tty(TTY_DEV)
        wire_fd = os.open(WIRE_DEV, os.O_RDONLY | os.O_NONBLOCK)
        try:
            msg = b"Hello, wire!"
            n = os.write(tty_fd, msg)
            assert n == len(msg)

            data = _read_with_timeout(wire_fd, len(msg))
            assert data == msg, f"expected {msg!r}, got {data!r}"
        finally:
            os.close(tty_fd)
            os.close(wire_fd)

    def test_stat_tx_bytes_increments(self, uart_module):
        tty_fd  = _open_raw_tty(TTY_DEV)
        wire_fd = os.open(WIRE_DEV, os.O_RDONLY | os.O_NONBLOCK)
        try:
            before = int(_read_sysfs(_uart_path(0, "stats/tx_bytes")))
            msg = b"stats"
            os.write(tty_fd, msg)
            _read_with_timeout(wire_fd, len(msg))   # drain so work_fn runs
            after = int(_read_sysfs(_uart_path(0, "stats/tx_bytes")))
            assert after >= before + len(msg)
        finally:
            os.close(tty_fd)
            os.close(wire_fd)


# ---------------------------------------------------------------------------
# AC4 — wire → AUT
# ---------------------------------------------------------------------------

class TestWireToAUT:
    """AC4 — data written by simulator appears on the AUT TTY."""

    def test_wire_write_aut_read(self, uart_module):
        # Open TTY first; flip buffer only works when the TTY port is active.
        tty_fd  = _open_raw_tty(TTY_DEV)
        wire_fd = os.open(WIRE_DEV, os.O_WRONLY | os.O_NONBLOCK)
        try:
            msg = b"From simulator"
            n = os.write(wire_fd, msg)
            assert n == len(msg)

            data = _read_with_timeout(tty_fd, len(msg))
            assert data == msg, f"expected {msg!r}, got {data!r}"
        finally:
            os.close(tty_fd)
            os.close(wire_fd)

    def test_stat_rx_bytes_increments(self, uart_module):
        tty_fd  = _open_raw_tty(TTY_DEV)
        wire_fd = os.open(WIRE_DEV, os.O_WRONLY | os.O_NONBLOCK)
        try:
            before = int(_read_sysfs(_uart_path(0, "stats/rx_bytes")))
            msg = b"rxstats"
            os.write(wire_fd, msg)
            _read_with_timeout(tty_fd, len(msg))
            after = int(_read_sysfs(_uart_path(0, "stats/rx_bytes")))
            assert after >= before + len(msg)
        finally:
            os.close(tty_fd)
            os.close(wire_fd)


# ---------------------------------------------------------------------------
# AC5 — buf_sz resize rejected while open
# ---------------------------------------------------------------------------

class TestBufResizeBusy:
    """AC5 — buf_sz sysfs attrs return EBUSY while TTY or wire is open."""

    def test_tx_buf_resize_blocked_by_wire(self, uart_module):
        fd = os.open(WIRE_DEV, os.O_RDONLY | os.O_NONBLOCK)
        try:
            with pytest.raises(OSError) as exc:
                _write_sysfs(_uart_path(0, "tx_buf_sz"), 512)
            assert exc.value.errno == errno.EBUSY
        finally:
            os.close(fd)

    def test_rx_buf_resize_blocked_by_wire(self, uart_module):
        fd = os.open(WIRE_DEV, os.O_RDONLY | os.O_NONBLOCK)
        try:
            with pytest.raises(OSError) as exc:
                _write_sysfs(_uart_path(0, "rx_buf_sz"), 512)
            assert exc.value.errno == errno.EBUSY
        finally:
            os.close(fd)

    def test_tx_buf_resize_blocked_by_tty(self, uart_module):
        fd = _open_raw_tty(TTY_DEV)
        try:
            with pytest.raises(OSError) as exc:
                _write_sysfs(_uart_path(0, "tx_buf_sz"), 512)
            assert exc.value.errno == errno.EBUSY
        finally:
            os.close(fd)

    def test_rx_buf_resize_blocked_by_tty(self, uart_module):
        fd = _open_raw_tty(TTY_DEV)
        try:
            with pytest.raises(OSError) as exc:
                _write_sysfs(_uart_path(0, "rx_buf_sz"), 512)
            assert exc.value.errno == errno.EBUSY
        finally:
            os.close(fd)

    def test_resize_allowed_when_closed(self, uart_module):
        """No fds open — resize must succeed."""
        _write_sysfs(_uart_path(0, "tx_buf_sz"), 512)
        assert _read_sysfs(_uart_path(0, "tx_buf_sz")) == "512"
        _write_sysfs(_uart_path(0, "tx_buf_sz"), 4096)


# ---------------------------------------------------------------------------
# AC6 — Bus reset signals wire side
# ---------------------------------------------------------------------------

class TestBusReset:
    """AC6 — bus RESET sends EOF to wire; fault attrs and stats cleared."""

    def test_wire_read_returns_eof_on_reset(self, uart_module):
        wire_fd = os.open(WIRE_DEV, os.O_RDONLY | os.O_NONBLOCK)
        try:
            _write_sysfs(BUS0_STATE, "reset")

            # After reset wire_poll returns EPOLLHUP; select sees it as readable.
            r, _, _ = select.select([wire_fd], [], [], READ_TIMEOUT)
            assert r, "wire_fd was not readable after bus reset (timeout)"

            data = os.read(wire_fd, 256)
            assert data == b"", "Expected EOF (empty bytes) from wire after reset"
        finally:
            os.close(wire_fd)

    def test_fault_attrs_cleared_on_reset(self, uart_module):
        _write_sysfs(_uart_path(0, "drop_rate_ppm"), 500)
        _write_sysfs(BUS0_STATE, "reset")
        assert _read_sysfs(_uart_path(0, "drop_rate_ppm")) == "0"
        assert _read_sysfs(_uart_path(0, "latency_ns")) == "0"
        assert _read_sysfs(_uart_path(0, "jitter_ns")) == "0"
        assert _read_sysfs(_uart_path(0, "bitflip_rate_ppm")) == "0"
        assert _read_sysfs(_uart_path(0, "enabled")) == "1"

    def test_stats_cleared_on_reset(self, uart_module):
        _write_sysfs(BUS0_STATE, "reset")
        assert _read_sysfs(_uart_path(0, "stats/tx_bytes")) == "0"
        assert _read_sysfs(_uart_path(0, "stats/rx_bytes")) == "0"
        assert _read_sysfs(_uart_path(0, "stats/drops")) == "0"
        assert _read_sysfs(_uart_path(0, "stats/overruns")) == "0"
