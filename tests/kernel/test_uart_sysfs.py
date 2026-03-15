"""
test_uart_sysfs.py — sysfs contract tests for virtrtlab_uart.

Acceptance criteria covered:

Issue #3 — per-device stats counters:
  AC1 — cat stats/tx_bytes returns the current value (readable, non-negative u64)
  AC2 — counters increment under load (requires issue #2 wire device — skipped)
  AC3 — echo 0 > stats/reset resets all counters to zero
  AC4 — echo 1 > stats/reset returns -EINVAL
  AC5 — stats/reset is write-only (read returns permission error)

Issue #4 — inline fault injection engine:
  AC1 — drop_rate_ppm=1000000: 100% drops (requires issue #2 wire device — skipped)
  AC2 — bitflip_rate_ppm=1000000: observable corruption (requires issue #2 — skipped)
  AC3 — latency_ns=1000000 under 115200 baud load: no soft lockup (requires #2 — skipped)
  AC4 — latency_ns=10000000001 returns -EINVAL
  AC5 — drop_rate_ppm=1000001 returns -EINVAL

Additional sysfs contract covered:
  - latency_ns/jitter_ns: exact ceiling (10 000 000 000) accepted; ceiling+1 rejected
  - bitflip_rate_ppm: exact ceiling (1 000 000) accepted; ceiling+1 rejected
  - all four fault attrs default to 0 on load and after bus reset
  - enabled defaults to 1; bus reset restores enabled=1 even if disabled
  - bus reset (state=reset) clears all fault attrs and stats
"""

import errno
import os
import select
import subprocess
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
READ_TIMEOUT = 2.0   # seconds


def _open_raw_tty(path):
    """Open a TTY in raw mode and return the fd."""
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    tty.setraw(fd, termios.TCSANOW)
    return fd


def _read_with_timeout(fd, nbytes, timeout=READ_TIMEOUT):
    """Read up to *nbytes* from *fd*, waiting at most *timeout* seconds."""
    buf = b""
    deadline = time.monotonic() + timeout
    while len(buf) < nbytes:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        ready, _, _ = select.select([fd], [], [], remaining)
        if not ready:
            break
        chunk = os.read(fd, nbytes - len(buf))
        if not chunk:
            break
        buf += chunk
    return buf


# ---------------------------------------------------------------------------
# Sysfs I/O helpers
# ---------------------------------------------------------------------------

def _uart_path(idx, attr=""):
    base = f"{DEVICES_ROOT}/uart{idx}"
    return os.path.join(base, attr) if attr else base


def r(path):
    """Read and strip a sysfs attribute."""
    with open(path) as f:
        return f.read().strip()


def w(path, value):
    """
    Write *value* to a sysfs path.
    Returns 0 on success, the OSError errno on failure.
    """
    try:
        with open(path, "w") as f:
            f.write(str(value))
        return 0
    except OSError as e:
        return e.errno


# Shortcuts for uart0
def u(attr=""):
    return _uart_path(0, attr)


# ---------------------------------------------------------------------------
# [Issue #3] Stats counters — readability and defaults
# ---------------------------------------------------------------------------

class TestUartStatsReadability:
    """Stats attributes are readable and return valid non-negative integers."""

    @pytest.mark.parametrize("counter", ["tx_bytes", "rx_bytes", "overruns", "drops"])
    def test_stat_is_readable(self, uart_module, counter):
        """cat stats/<counter> must return a valid non-negative integer (AC1)."""
        val = r(u(f"stats/{counter}"))
        assert val.isdigit(), (
            f"stats/{counter} must be a non-negative integer, got {val!r}"
        )

    @pytest.mark.parametrize("counter", ["tx_bytes", "rx_bytes", "overruns", "drops"])
    def test_stat_default_is_zero(self, uart_module, counter):
        """All stats counters must read 0 immediately after load."""
        assert r(u(f"stats/{counter}")) == "0", \
            f"stats/{counter} must be 0 after fresh load"


# ---------------------------------------------------------------------------
# [Issue #3 AC3/AC4] Stats reset attribute
# ---------------------------------------------------------------------------

class TestUartStatsReset:
    """stats/reset write semantics (issue #3)."""

    def test_reset_write_zero_succeeds(self, uart_module):
        """echo 0 > stats/reset must be accepted (AC3)."""
        assert w(u("stats/reset"), "0") == 0, \
            "Writing '0' to stats/reset must succeed"

    def test_reset_clears_all_counters(self, uart_module):
        """After echo 0 > stats/reset, all counters must read 0 (AC3).

        Counters are already 0 at load; we verify the write path still works
        and that all counters remain 0 (the actual increment path requires
        the issue #2 wire device).
        """
        assert w(u("stats/reset"), "0") == 0
        for counter in ("tx_bytes", "rx_bytes", "overruns", "drops"):
            assert r(u(f"stats/{counter}")) == "0", \
                f"stats/{counter} must be 0 after reset"

    def test_reset_write_one_returns_einval(self, uart_module):
        """echo 1 > stats/reset must return -EINVAL (AC4)."""
        assert w(u("stats/reset"), "1") == errno.EINVAL, \
            "Writing '1' to stats/reset must return EINVAL"

    @pytest.mark.parametrize("bad_val", ["2", "42", "999999", "4294967295"])
    def test_reset_write_nonzero_returns_einval(self, uart_module, bad_val):
        """Any non-zero write to stats/reset must return -EINVAL (AC4)."""
        assert w(u("stats/reset"), bad_val) == errno.EINVAL, \
            f"Writing '{bad_val}' to stats/reset must return EINVAL"

    def test_reset_is_write_only(self, uart_module):
        """Reading stats/reset must fail with EPERM or EACCES (AC5)."""
        with pytest.raises(OSError) as exc_info:
            with open(u("stats/reset")) as f:
                f.read()
        assert exc_info.value.errno in (errno.EPERM, errno.EACCES), (
            f"Expected EPERM or EACCES reading stats/reset, got "
            f"errno={exc_info.value.errno}"
        )


# ---------------------------------------------------------------------------
# [Issue #3 AC2] Counter increment under load — requires issue #2 wire device
# ---------------------------------------------------------------------------

class TestUartStatsLoopback:
    """Stats counter increment under TX/RX load (issue #3 AC2)."""

    def test_tx_bytes_increments_under_load(self, uart_module):
        """tx_bytes must grow when bytes are sent through the wire device."""
        tty_fd  = _open_raw_tty(TTY_DEV)
        wire_fd = os.open(WIRE_DEV, os.O_RDWR | os.O_NONBLOCK)
        try:
            os.write(tty_fd, b"\xAA" * 64)
            time.sleep(0.5)
            tx = int(r(u("stats/tx_bytes")))
            assert tx >= 64, f"stats/tx_bytes={tx} expected >= 64"
        finally:
            os.close(wire_fd)
            os.close(tty_fd)

    def test_rx_bytes_increments_under_load(self, uart_module):
        """rx_bytes must grow when bytes arrive from the wire device."""
        tty_fd  = _open_raw_tty(TTY_DEV)
        wire_fd = os.open(WIRE_DEV, os.O_RDWR | os.O_NONBLOCK)
        try:
            os.write(wire_fd, b"\xBB" * 64)
            time.sleep(0.1)
            rx = int(r(u("stats/rx_bytes")))
            assert rx >= 64, f"stats/rx_bytes={rx} expected >= 64"
        finally:
            os.close(wire_fd)
            os.close(tty_fd)

    def test_drops_count_matches_drop_rate(self, uart_module):
        """drops counter must match TX bytes when drop_rate_ppm=1000000."""
        w(u("drop_rate_ppm"), "1000000")
        tty_fd  = _open_raw_tty(TTY_DEV)
        wire_fd = os.open(WIRE_DEV, os.O_RDWR | os.O_NONBLOCK)
        try:
            os.write(tty_fd, b"\xCC" * 64)
            time.sleep(0.5)
            tx    = int(r(u("stats/tx_bytes")))
            drops = int(r(u("stats/drops")))
            assert tx >= 64, f"stats/tx_bytes={tx}"
            assert drops == tx, (
                f"drops={drops} must equal tx_bytes={tx} with drop_rate_ppm=1000000"
            )
        finally:
            os.close(wire_fd)
            os.close(tty_fd)
            w(u("drop_rate_ppm"), "0")


# ---------------------------------------------------------------------------
# [Issue #4] Fault injection — attribute defaults
# ---------------------------------------------------------------------------

class TestUartFaultInjectionDefaults:
    """All fault injection attributes default to 0 at load time (issue #4)."""

    @pytest.mark.parametrize("attr", [
        "latency_ns", "jitter_ns", "drop_rate_ppm", "bitflip_rate_ppm",
    ])
    def test_defaults_to_zero(self, uart_module, attr):
        """Fault injection attribute must be 0 at load time."""
        assert r(u(attr)) == "0", f"{attr} must default to 0"


# ---------------------------------------------------------------------------
# [Issue #4 AC4/AC5] Fault injection — range validation
# ---------------------------------------------------------------------------

class TestUartFaultInjectionRanges:
    """Range boundaries for latency_ns, jitter_ns, drop_rate_ppm, bitflip_rate_ppm."""

    # latency_ns / jitter_ns: ceiling = 10 000 000 000 ns (10 s)

    def test_latency_ns_zero_accepted(self, uart_module):
        """latency_ns=0 must be accepted."""
        assert w(u("latency_ns"), "0") == 0

    def test_latency_ns_max_boundary_accepted(self, uart_module):
        """latency_ns=10000000000 (ceiling) must be accepted and read back."""
        assert w(u("latency_ns"), "10000000000") == 0
        assert r(u("latency_ns")) == "10000000000"

    def test_latency_ns_over_max_returns_einval(self, uart_module):
        """latency_ns=10000000001 must return -EINVAL (issue #4 AC4)."""
        assert w(u("latency_ns"), "10000000001") == errno.EINVAL, \
            "latency_ns=10000000001 must return EINVAL"

    def test_jitter_ns_max_boundary_accepted(self, uart_module):
        """jitter_ns=10000000000 (ceiling) must be accepted and read back."""
        assert w(u("jitter_ns"), "10000000000") == 0
        assert r(u("jitter_ns")) == "10000000000"

    def test_jitter_ns_over_max_returns_einval(self, uart_module):
        """jitter_ns=10000000001 must return -EINVAL."""
        assert w(u("jitter_ns"), "10000000001") == errno.EINVAL, \
            "jitter_ns=10000000001 must return EINVAL"

    def test_drop_rate_ppm_max_boundary_accepted(self, uart_module):
        """drop_rate_ppm=1000000 (ceiling) must be accepted and read back."""
        assert w(u("drop_rate_ppm"), "1000000") == 0
        assert r(u("drop_rate_ppm")) == "1000000"

    def test_drop_rate_ppm_over_max_returns_einval(self, uart_module):
        """drop_rate_ppm=1000001 must return -EINVAL (issue #4 AC5)."""
        assert w(u("drop_rate_ppm"), "1000001") == errno.EINVAL, \
            "drop_rate_ppm=1000001 must return EINVAL"

    def test_bitflip_rate_ppm_max_boundary_accepted(self, uart_module):
        """bitflip_rate_ppm=1000000 (ceiling) must be accepted and read back."""
        assert w(u("bitflip_rate_ppm"), "1000000") == 0
        assert r(u("bitflip_rate_ppm")) == "1000000"

    def test_bitflip_rate_ppm_over_max_returns_einval(self, uart_module):
        """bitflip_rate_ppm=1000001 must return -EINVAL."""
        assert w(u("bitflip_rate_ppm"), "1000001") == errno.EINVAL, \
            "bitflip_rate_ppm=1000001 must return EINVAL"

    def test_latency_ns_accepts_mid_range(self, uart_module):
        """latency_ns=1000000 (1 ms) must be accepted and read back."""
        assert w(u("latency_ns"), "1000000") == 0
        assert r(u("latency_ns")) == "1000000"

    def test_drop_rate_ppm_accepts_zero(self, uart_module):
        """drop_rate_ppm=0 must be accepted (no drops)."""
        assert w(u("drop_rate_ppm"), "0") == 0


# ---------------------------------------------------------------------------
# [Issue #4 AC1-AC3] Fault injection behavior — requires issue #2 wire device
# ---------------------------------------------------------------------------

class TestUartFaultInjectionBehavior:
    """Runtime fault injection behavior (issue #4 AC1–AC3)."""

    def test_drop_rate_ppm_full_drops_all_bytes(self, uart_module):
        """drop_rate_ppm=1000000: 100% of TX bytes must appear in stats/drops (AC1)."""
        w(u("drop_rate_ppm"), "1000000")
        tty_fd  = _open_raw_tty(TTY_DEV)
        wire_fd = os.open(WIRE_DEV, os.O_RDWR | os.O_NONBLOCK)
        try:
            os.write(tty_fd, b"\xAA" * 64)
            time.sleep(0.5)
            tx    = int(r(u("stats/tx_bytes")))
            drops = int(r(u("stats/drops")))
            assert tx >= 64, f"stats/tx_bytes={tx}"
            assert drops == tx, (
                f"drops={drops} must equal tx_bytes={tx} with drop_rate_ppm=1000000 (AC1)"
            )
        finally:
            os.close(wire_fd)
            os.close(tty_fd)
            w(u("drop_rate_ppm"), "0")

    def test_bitflip_rate_ppm_full_corrupts_stream(self, uart_module):
        """bitflip_rate_ppm=1000000: wire device observes bit-flipped bytes (AC2)."""
        w(u("bitflip_rate_ppm"), "1000000")
        tty_fd  = _open_raw_tty(TTY_DEV)
        wire_fd = os.open(WIRE_DEV, os.O_RDWR | os.O_NONBLOCK)
        try:
            payload = b"\x00" * 64
            os.write(tty_fd, payload)
            received = _read_with_timeout(wire_fd, len(payload))
            assert len(received) > 0, "No bytes received on wire side"
            assert received != payload[:len(received)], (
                "With bitflip_rate_ppm=1000000, received bytes must differ from sent (AC2)"
            )
        finally:
            os.close(wire_fd)
            os.close(tty_fd)
            w(u("bitflip_rate_ppm"), "0")

    def test_latency_ns_1ms_no_soft_lockup(self, uart_module):
        """latency_ns=1000000 at 115200 baud: dmesg must not show soft lockup (AC3)."""
        w(u("latency_ns"), "1000000")
        tty_fd  = _open_raw_tty(TTY_DEV)
        wire_fd = os.open(WIRE_DEV, os.O_RDWR | os.O_NONBLOCK)
        attrs = termios.tcgetattr(tty_fd)
        attrs[4] = attrs[5] = termios.B115200
        termios.tcsetattr(tty_fd, termios.TCSANOW, attrs)
        try:
            for _ in range(10):
                os.write(tty_fd, b"\xAA" * 64)
                time.sleep(0.05)
            time.sleep(1.0)
            result = subprocess.run(["dmesg"], capture_output=True, text=True,
                                    check=False)
            assert "soft lockup" not in result.stdout.lower(), (
                "Detected soft lockup in dmesg with latency_ns=1000000 (AC3)"
            )
        finally:
            os.close(wire_fd)
            os.close(tty_fd)
            w(u("latency_ns"), "0")


# ---------------------------------------------------------------------------
# Bus reset — notifier integration (clears fault attrs, stats, restores enabled)
# ---------------------------------------------------------------------------

class TestUartBusReset:
    """Bus state=reset must clear fault attrs and stats via the notifier (issue #3/#4)."""

    def test_fault_attrs_cleared_on_bus_reset(self, uart_module):
        """After state=reset, all fault attrs must return to 0."""
        w(u("latency_ns"),       "5000000")
        w(u("jitter_ns"),        "1000000")
        w(u("drop_rate_ppm"),    "500000")
        w(u("bitflip_rate_ppm"), "100000")

        assert w(BUS0_STATE, "reset") == 0

        assert r(u("latency_ns"))       == "0", "latency_ns must be 0 after bus reset"
        assert r(u("jitter_ns"))        == "0", "jitter_ns must be 0 after bus reset"
        assert r(u("drop_rate_ppm"))    == "0", "drop_rate_ppm must be 0 after bus reset"
        assert r(u("bitflip_rate_ppm")) == "0", "bitflip_rate_ppm must be 0 after bus reset"

    def test_enabled_restored_on_bus_reset(self, uart_module):
        """After state=reset, enabled must return to 1 even if it was 0."""
        assert w(u("enabled"), "0") == 0
        assert r(u("enabled")) == "0"

        assert w(BUS0_STATE, "reset") == 0

        assert r(u("enabled")) == "1", "enabled must be 1 after bus reset"

    @pytest.mark.parametrize("counter", ["tx_bytes", "rx_bytes", "overruns", "drops"])
    def test_stats_cleared_on_bus_reset(self, uart_module, counter):
        """After state=reset, all stats counters must read 0."""
        # Counters are already 0 at load (no wire device yet); reset is a
        # no-op on values but exercises the atomic64_set code path.
        assert w(BUS0_STATE, "reset") == 0
        assert r(u(f"stats/{counter}")) == "0", \
            f"stats/{counter} must be 0 after bus reset"
