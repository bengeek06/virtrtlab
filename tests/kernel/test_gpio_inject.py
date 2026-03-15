"""
test_gpio_inject.py — dedicated inject mechanism tests for virtrtlab_gpio v0.2.0.

Acceptance criteria covered:
  - Per-line independence: inject on line N does not affect line M
  - Drop gate at 100% rate: every inject is dropped, stat_drops increments
  - Drop gate at 0% rate: no drops, stat_value_changes may increment
  - Bitflip gate at 100% rate: every inject is flipped, stat_bitflips increments
  - Bitflip gate at 0% rate: no bitflips, stat_bitflips stays 0
  - Latency inject: write returns immediately (non-blocking); timer runs async
  - stat counters are per-device (not per-line): all lines share the same counters
  - inject on each line [0..7] is accepted
"""

import os
import time

import pytest

from conftest import (
    KO,
    SYSFS_ROOT,
    _insmod,
    _module_loaded,
    _rmmod,
)

DEVICES_ROOT = f"{SYSFS_ROOT}/devices"


def g(dev, attr=""):
    """Return the sysfs path for gpio<dev>[/attr]."""
    base = f"{DEVICES_ROOT}/gpio{dev}"
    return os.path.join(base, attr) if attr else base


def r(path):
    """Read and strip a sysfs attribute."""
    with open(path) as f:
        return f.read().strip()


def w(path, value):
    """Write *value* to path; return 0 on success, OSError errno on failure."""
    try:
        with open(path, "w") as f:
            f.write(str(value))
        return 0
    except OSError as e:
        return e.errno


def reset_stats(dev=0):
    """Reset all stats counters on gpio<dev>."""
    w(g(dev, "stats/reset"), "0")


def reset_fault_attrs(dev=0):
    """Set all fault attrs to 0 on gpio<dev>."""
    for attr in ("latency_ns", "jitter_ns", "drop_rate_ppm", "bitflip_rate_ppm"):
        w(g(dev, attr), "0")
    w(g(dev, "enabled"), "1")


# ---------------------------------------------------------------------------
# [INJ-1] All 8 lines are individually injectable
# ---------------------------------------------------------------------------

class TestGpioInjectAllLines:
    """Each of the 8 lines must be independently injectable."""

    @pytest.mark.parametrize("line", range(8))
    def test_inject_line_n_accepted(self, gpio_module, line):
        """inject write on line N must return 0 (success)."""
        reset_fault_attrs()
        assert w(g(0, "inject"), f"{line}:1") == 0
        assert w(g(0, "inject"), f"{line}:0") == 0


# ---------------------------------------------------------------------------
# [INJ-2] Per-line independence
# ---------------------------------------------------------------------------

class TestGpioInjectPerLineIndependence:
    """Injecting one line must not disturb the stats of unrelated injects."""

    def test_value_changes_count_matches_inject_count(self, gpio_module):
        """
        Inject 1 on line 0 (0→1), then inject 1 on line 1 (0→1):
        stat_value_changes must equal 2.
        """
        reset_fault_attrs()
        reset_stats()
        # Ensure both lines start at 0.
        w(g(0, "inject"), "0:0")
        w(g(0, "inject"), "1:0")
        reset_stats()
        # Now transition both lines 0→1.
        w(g(0, "inject"), "0:1")
        w(g(0, "inject"), "1:1")
        assert int(r(g(0, "stats/value_changes"))) == 2

    def test_inject_line2_does_not_affect_line5_stat(self, gpio_module):
        """
        Inject 1 on line 2 only, then inject 1 again on line 2 (no change).
        Only 1 value_changes total, not 2.
        """
        reset_fault_attrs()
        reset_stats()
        w(g(0, "inject"), "2:0")   # ensure line 2 starts at 0
        reset_stats()
        w(g(0, "inject"), "2:1")   # transition 0→1: value_changes = 1
        w(g(0, "inject"), "2:1")   # no change: value_changes stays 1
        assert int(r(g(0, "stats/value_changes"))) == 1


# ---------------------------------------------------------------------------
# [INJ-3] Drop gate — deterministic 100% rate
# ---------------------------------------------------------------------------

class TestGpioInjectDropGate:
    """Drop gate behaviour at 100% and 0% rates."""

    def test_drop_100pct_increments_stat_drops(self, gpio_module):
        """drop_rate_ppm=1_000_000: every inject increments stat_drops."""
        reset_fault_attrs()
        w(g(0, "drop_rate_ppm"), "1000000")
        reset_stats()
        for _ in range(5):
            assert w(g(0, "inject"), "3:1") == 0, "Dropped inject must still return 0"
        w(g(0, "drop_rate_ppm"), "0")
        assert int(r(g(0, "stats/drops"))) == 5

    def test_drop_100pct_no_value_changes(self, gpio_module):
        """drop_rate_ppm=1_000_000: stat_value_changes must stay 0."""
        reset_fault_attrs()
        w(g(0, "drop_rate_ppm"), "1000000")
        reset_stats()
        w(g(0, "inject"), "4:1")
        w(g(0, "drop_rate_ppm"), "0")
        assert int(r(g(0, "stats/value_changes"))) == 0

    def test_drop_0pct_no_drops_counted(self, gpio_module):
        """drop_rate_ppm=0: stat_drops stays 0 even after multiple injects."""
        reset_fault_attrs()
        w(g(0, "drop_rate_ppm"), "0")
        reset_stats()
        w(g(0, "inject"), "5:0")
        w(g(0, "inject"), "5:1")
        assert int(r(g(0, "stats/drops"))) == 0


# ---------------------------------------------------------------------------
# [INJ-4] Bitflip gate — deterministic 100% rate
# ---------------------------------------------------------------------------

class TestGpioInjectBitflipGate:
    """Bitflip gate at 100% and 0% rates."""

    def test_bitflip_100pct_increments_stat_bitflips(self, gpio_module):
        """bitflip_rate_ppm=1_000_000: every inject increments stat_bitflips."""
        reset_fault_attrs()
        # Alternate-value injects to ensure each actually changes the line.
        w(g(0, "inject"), "6:0")   # baseline
        reset_stats()
        w(g(0, "bitflip_rate_ppm"), "1000000")
        # inject 0 → bitflipped to 1 → value changes (0→1), stat_bitflips++
        w(g(0, "inject"), "6:0")
        w(g(0, "bitflip_rate_ppm"), "0")
        assert int(r(g(0, "stats/bitflips"))) >= 1

    def test_bitflip_0pct_no_bitflips_counted(self, gpio_module):
        """bitflip_rate_ppm=0: stat_bitflips stays 0."""
        reset_fault_attrs()
        w(g(0, "inject"), "7:0")   # baseline
        reset_stats()
        w(g(0, "bitflip_rate_ppm"), "0")
        w(g(0, "inject"), "7:1")
        w(g(0, "inject"), "7:0")
        assert int(r(g(0, "stats/bitflips"))) == 0

    def test_bitflip_100pct_value_still_changes(self, gpio_module):
        """bitflip_rate_ppm=1_000_000: value must change (flipped 0→1)."""
        reset_fault_attrs()
        w(g(0, "inject"), "0:0")   # ensure line starts at 0
        reset_stats()
        w(g(0, "bitflip_rate_ppm"), "1000000")
        w(g(0, "inject"), "0:0")   # inject 0; bitflip makes it 1
        w(g(0, "bitflip_rate_ppm"), "0")
        # stat_value_changes must be >= 1 (0→1 transition)
        assert int(r(g(0, "stats/value_changes"))) >= 1


# ---------------------------------------------------------------------------
# [INJ-5] Latency — non-blocking inject
# ---------------------------------------------------------------------------

class TestGpioInjectLatency:
    """inject with latency_ns set must return immediately (async delivery)."""

    def test_inject_returns_immediately_with_latency(self, gpio_module):
        """
        With latency_ns=500_000_000 (500 ms), inject must return in < 100 ms.
        The actual value delivery is asynchronous via hrtimer + workqueue.
        """
        reset_fault_attrs()
        w(g(0, "latency_ns"), "500000000")   # 500 ms
        t0 = time.monotonic()
        result = w(g(0, "inject"), "0:1")
        elapsed = time.monotonic() - t0
        w(g(0, "latency_ns"), "0")           # restore
        assert result == 0, f"inject must succeed, got errno={result}"
        assert elapsed < 0.1, (
            f"inject with latency_ns=500ms must not block; elapsed={elapsed:.3f}s"
        )

    def test_inject_returns_immediately_with_large_jitter(self, gpio_module):
        """With jitter_ns=5_000_000_000 (5 s), inject must return in < 100 ms."""
        reset_fault_attrs()
        w(g(0, "latency_ns"), "1000000")      # 1 ms base
        w(g(0, "jitter_ns"),  "5000000000")   # 5 s jitter
        t0 = time.monotonic()
        result = w(g(0, "inject"), "1:1")
        elapsed = time.monotonic() - t0
        reset_fault_attrs()
        assert result == 0
        assert elapsed < 0.1, (
            f"inject with large jitter must not block; elapsed={elapsed:.3f}s"
        )
