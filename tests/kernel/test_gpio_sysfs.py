"""
test_gpio_sysfs.py — sysfs contract tests for virtrtlab_gpio v0.2.0.

Acceptance criteria covered:
  - inject attr format: "N:V" where N in [0,7], V in {0,1}; bad formats return -EINVAL/-ERANGE
  - inject write-only: reading inject returns permission error
  - inject blocked when enabled=0: silently discarded, returns count
  - inject blocked when bus is down: returns -EIO
  - latency_ns/jitter_ns > 10_000_000_000 returns -EINVAL; exact max accepted
  - drop_rate_ppm/bitflip_rate_ppm > 1_000_000 returns -EINVAL; exact max accepted
  - stats/value_changes counts per-line value transitions applied
  - stats/drops counts inject writes suppressed by drop_rate_ppm
  - stats/bitflips counts bitflip gate fires
  - stats/reset write "0" clears all counters atomically
  - stats/reset write non-zero returns -EINVAL
  - stats/reset read returns permission error (write-only attr)
  - bus state=reset: clears fault attrs and stats; sets enabled=1
"""

import errno
import os

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


# ---------------------------------------------------------------------------
# Sysfs I/O helpers
# ---------------------------------------------------------------------------

def g(dev, attr=""):
    """Return the sysfs path for gpio<dev>[/attr]."""
    base = f"{DEVICES_ROOT}/gpio{dev}"
    return os.path.join(base, attr) if attr else base


def r(path):
    """Read and strip a sysfs attribute."""
    with open(path) as f:
        return f.read().strip()


def w(path, value):
    """
    Write *value* (str) to a sysfs path.
    Returns 0 on success, the OSError errno on failure.
    """
    try:
        with open(path, "w") as f:
            f.write(str(value))
        return 0
    except OSError as e:
        return e.errno


# ---------------------------------------------------------------------------
# [AC-1] inject attr format validation
# ---------------------------------------------------------------------------

class TestGpioInjectFormat:
    """inject attr enforces strict 'N:V' format."""

    INVALID = [
        "8:0",       # N out of range
        "9:1",       # N out of range
        "0:2",       # V not 0 or 1
        "0:a",       # V non-numeric
        "0:",        # missing V
        ":0",        # missing N
        "0",         # missing separator
        "0 :0",      # space in N
        "0x0:1",     # hex prefix for N
    ]

    VALID = [
        ("0:0", 0), ("0:1", 0),
        ("7:0", 0), ("7:1", 0),
        ("3:0", 0), ("4:1", 0),
    ]

    @pytest.mark.parametrize("bad_val", INVALID)
    def test_bad_format_rejected(self, gpio_module, bad_val):
        """Any malformed inject value must return an error."""
        result = w(g(0, "inject"), bad_val)
        assert result != 0, (
            f"Expected error for inject={bad_val!r}, got success"
        )

    @pytest.mark.parametrize("good_val,expected_ret", VALID)
    def test_valid_format_accepted(self, gpio_module, good_val, expected_ret):
        """Valid 'N:V' strings must succeed (return 0)."""
        result = w(g(0, "inject"), good_val)
        assert result == expected_ret, (
            f"Expected {expected_ret} for inject={good_val!r}, got errno={result}"
        )

    def test_inject_is_write_only(self, gpio_module):
        """Reading inject must raise PermissionError (write-only attr)."""
        with pytest.raises(PermissionError):
            with open(g(0, "inject")) as f:
                f.read()


# ---------------------------------------------------------------------------
# [AC-2] inject gate behaviour
# ---------------------------------------------------------------------------

class TestGpioInjectGates:
    """enabled=0 and bus state=down gate inject writes."""

    def test_inject_silently_discarded_when_disabled(self, gpio_module):
        """inject with enabled=0 must return count (success), write discarded."""
        w(g(0, "enabled"), "0")
        result = w(g(0, "inject"), "0:1")
        w(g(0, "enabled"), "1")        # restore before assert
        assert result == 0, (
            f"inject with enabled=0 must return success (count), got errno={result}"
        )

    def test_inject_allowed_when_reenabled(self, gpio_module):
        """inject must succeed after re-enabling."""
        w(g(0, "enabled"), "0")
        w(g(0, "enabled"), "1")
        result = w(g(0, "inject"), "0:1")
        assert result == 0

    def test_inject_blocked_when_bus_down(self, gpio_module):
        """inject must return EIO when bus is down."""
        w(BUS0_STATE, "down")
        result = w(g(0, "inject"), "0:1")
        w(BUS0_STATE, "up")            # restore before assert
        assert result == errno.EIO, (
            f"Expected EIO with bus down, got errno={result}"
        )

    def test_inject_allowed_after_bus_up(self, gpio_module):
        """inject must succeed after bus comes back up."""
        w(BUS0_STATE, "down")
        w(BUS0_STATE, "up")
        result = w(g(0, "inject"), "0:1")
        assert result == 0

    def test_enabled_write_invalid_returns_error(self, gpio_module):
        """Writing a non-boolean to enabled must fail."""
        result = w(g(0, "enabled"), "2")
        assert result != 0, "Expected error for enabled=2"


# ---------------------------------------------------------------------------
# [AC-3] Fault attribute bounds
# ---------------------------------------------------------------------------

class TestGpioFaultAttrBounds:
    MAX_LATENCY = 10_000_000_000
    MAX_PPM     = 1_000_000

    @pytest.mark.parametrize("attr", ["latency_ns", "jitter_ns"])
    def test_latency_at_max_accepted(self, gpio_module, attr):
        result = w(g(0, attr), str(self.MAX_LATENCY))
        # reset immediately so no side-effect on other tests in this call
        w(g(0, attr), "0")
        assert result == 0, f"{attr}={self.MAX_LATENCY}: expected success"

    @pytest.mark.parametrize("attr", ["latency_ns", "jitter_ns"])
    def test_latency_over_max_rejected(self, gpio_module, attr):
        result = w(g(0, attr), str(self.MAX_LATENCY + 1))
        assert result == errno.EINVAL, (
            f"{attr}={self.MAX_LATENCY + 1}: expected EINVAL"
        )

    @pytest.mark.parametrize("attr", ["drop_rate_ppm", "bitflip_rate_ppm"])
    def test_ppm_at_max_accepted(self, gpio_module, attr):
        result = w(g(0, attr), str(self.MAX_PPM))
        w(g(0, attr), "0")
        assert result == 0, f"{attr}={self.MAX_PPM}: expected success"

    @pytest.mark.parametrize("attr", ["drop_rate_ppm", "bitflip_rate_ppm"])
    def test_ppm_over_max_rejected(self, gpio_module, attr):
        result = w(g(0, attr), str(self.MAX_PPM + 1))
        assert result == errno.EINVAL, (
            f"{attr}={self.MAX_PPM + 1}: expected EINVAL"
        )


# ---------------------------------------------------------------------------
# [AC-4] Stats counter semantics
# ---------------------------------------------------------------------------

class TestGpioStats:
    """stats/value_changes, stats/drops, stats/bitflips semantics."""

    def _reset(self):
        w(g(0, "stats/reset"), "0")

    def test_value_changes_increments_on_new_value(self, gpio_module):
        """Injecting a different value must increment value_changes."""
        self._reset()
        # From initial state (all zeros), inject 1 on line 0 — must transition 0→1.
        w(g(0, "inject"), "0:1")
        assert int(r(g(0, "stats/value_changes"))) == 1

    def test_value_changes_no_increment_on_same_value(self, gpio_module):
        """Injecting the same value twice must not increment value_changes."""
        # Force line 1 to 1, then inject 1 again.
        w(g(0, "inject"), "1:1")
        self._reset()
        w(g(0, "inject"), "1:1")
        assert int(r(g(0, "stats/value_changes"))) == 0

    def test_drops_increments_on_full_drop_rate(self, gpio_module):
        """drop_rate_ppm=1_000_000 (100%): every inject write is dropped."""
        w(g(0, "drop_rate_ppm"), "1000000")
        self._reset()
        result = w(g(0, "inject"), "2:1")
        w(g(0, "drop_rate_ppm"), "0")   # restore
        assert result == 0, "sysfs write must return success even on drop"
        assert int(r(g(0, "stats/drops"))) == 1

    def test_drops_not_incremented_at_zero_rate(self, gpio_module):
        """drop_rate_ppm=0: no drops."""
        w(g(0, "drop_rate_ppm"), "0")
        self._reset()
        w(g(0, "inject"), "3:1")
        assert int(r(g(0, "stats/drops"))) == 0

    def test_bitflips_increments_on_full_bitflip_rate(self, gpio_module):
        """bitflip_rate_ppm=1_000_000 (100%): every inject triggers a bitflip."""
        # Start from a known state: inject 0:0 first (ensure line 0 is 0).
        w(g(0, "bitflip_rate_ppm"), "0")
        w(g(0, "inject"), "0:0")
        self._reset()
        # Now enable 100% bitflip; inject 0:0 → bitflipped to 1 → value changes.
        w(g(0, "bitflip_rate_ppm"), "1000000")
        w(g(0, "inject"), "0:0")
        w(g(0, "bitflip_rate_ppm"), "0")   # restore
        assert int(r(g(0, "stats/bitflips"))) >= 1


# ---------------------------------------------------------------------------
# [AC-5] stats/reset contract
# ---------------------------------------------------------------------------

class TestGpioStatsReset:
    """stats/reset write/read contract."""

    def test_reset_clears_all_counters_atomically(self, gpio_module):
        """Write 0 to stats/reset must clear value_changes, drops, bitflips."""
        # Generate some counter activity.
        w(g(0, "inject"), "0:1")   # → value_changes
        assert int(r(g(0, "stats/value_changes"))) > 0
        w(g(0, "stats/reset"), "0")
        assert r(g(0, "stats/value_changes")) == "0"
        assert r(g(0, "stats/drops"))         == "0"
        assert r(g(0, "stats/bitflips"))      == "0"

    @pytest.mark.parametrize("bad_val", ["1", "255", "2"])
    def test_reset_rejects_nonzero(self, gpio_module, bad_val):
        """Writing any value other than 0 must return EINVAL."""
        result = w(g(0, "stats/reset"), bad_val)
        assert result == errno.EINVAL, (
            f"Expected EINVAL for stats/reset={bad_val!r}, got errno={result}"
        )

    def test_reset_read_returns_permission_error(self, gpio_module):
        """Reading stats/reset must raise PermissionError (write-only attr)."""
        with pytest.raises(PermissionError):
            with open(g(0, "stats/reset")) as f:
                f.read()


# ---------------------------------------------------------------------------
# [AC-6] bus state=reset semantics on GPIO device
# ---------------------------------------------------------------------------

class TestGpioBusReset:
    """Bus state=reset clears fault attrs and stats; sets enabled=1."""

    def test_bus_reset_clears_latency_ns(self, gpio_module):
        w(g(0, "latency_ns"), "500000")
        w(BUS0_STATE, "reset")
        assert r(g(0, "latency_ns")) == "0"

    def test_bus_reset_clears_jitter_ns(self, gpio_module):
        w(g(0, "jitter_ns"), "100000")
        w(BUS0_STATE, "reset")
        assert r(g(0, "jitter_ns")) == "0"

    def test_bus_reset_clears_drop_rate_ppm(self, gpio_module):
        w(g(0, "drop_rate_ppm"), "1000")
        w(BUS0_STATE, "reset")
        assert r(g(0, "drop_rate_ppm")) == "0"

    def test_bus_reset_clears_bitflip_rate_ppm(self, gpio_module):
        w(g(0, "bitflip_rate_ppm"), "500")
        w(BUS0_STATE, "reset")
        assert r(g(0, "bitflip_rate_ppm")) == "0"

    def test_bus_reset_clears_stats(self, gpio_module):
        """Bus reset must zero value_changes, drops, bitflips."""
        w(g(0, "inject"), "0:1")   # generate value_changes
        assert int(r(g(0, "stats/value_changes"))) > 0
        w(BUS0_STATE, "reset")
        assert r(g(0, "stats/value_changes")) == "0"
        assert r(g(0, "stats/drops"))         == "0"
        assert r(g(0, "stats/bitflips"))      == "0"

    def test_bus_reset_sets_enabled_true(self, gpio_module):
        """Bus reset must restore enabled=1 even when it was 0."""
        w(g(0, "enabled"), "0")
        assert r(g(0, "enabled")) == "0"
        w(BUS0_STATE, "reset")
        assert r(g(0, "enabled")) == "1"

    def test_bus_state_reads_up_after_reset(self, gpio_module):
        """Bus state attribute must read 'up' after state=reset."""
        w(BUS0_STATE, "reset")
        assert r(BUS0_STATE) == "up"

    def test_inject_works_after_bus_reset_from_disabled(self, gpio_module):
        """reset sets enabled=1: inject must succeed afterwards."""
        w(g(0, "enabled"), "0")
        w(BUS0_STATE, "reset")         # restores enabled=1
        result = w(g(0, "inject"), "0:1")
        assert result == 0


# ---------------------------------------------------------------------------
# [AC-7] inject with latency/jitter — timer regression
# ---------------------------------------------------------------------------

class TestGpioLargeJitter:
    """
    Regression: jitter_ns in the range (UINT32_MAX, MAX_LATENCY_NS] must
    not be silently biased.  We verify acceptance and basic functionality;
    statistical distribution correctness is not tested here.
    """

    def test_jitter_above_u32_max_accepted_and_readable(self, gpio_module):
        """jitter_ns=5_000_000_000 (> UINT32_MAX ~4.3e9) is writable and reads back."""
        w(g(0, "jitter_ns"), "5000000000")
        assert r(g(0, "jitter_ns")) == "5000000000"

    def test_inject_accepted_with_large_jitter(self, gpio_module):
        """An inject write with jitter_ns > UINT32_MAX must be accepted (no EINVAL)."""
        w(g(0, "latency_ns"), "1000000")    # 1 ms base
        w(g(0, "jitter_ns"),  "5000000000") # 5 s amplitude — timer fires async
        result = w(g(0, "inject"), "0:1")
        assert result == 0, "inject with large jitter must succeed"
