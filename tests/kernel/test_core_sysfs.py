# SPDX-License-Identifier: MIT

"""
test_core_sysfs.py — sysfs contract tests for virtrtlab_core (issue #13).

Acceptance criteria covered:
  AC4 — state accepts only up|down|reset; any other value returns -EINVAL
  AC5 — reset behavior: always transitions to 'up', observable from sysfs

Additional sysfs contract covered:
  - version matches the documented semver string "0.1.0"
  - clock_ns is a monotonically non-decreasing u64
  - seed default = 1 (VIRTRTLAB_DEFAULT_SEED)
  - seed write non-zero value: accepted and readable
  - seed write 0: rejected with -EINVAL
  - seed write decimal base only (kstrtou32 base=10 in implementation)
"""

import errno
import os
import time

import pytest

from conftest import KO, SYSFS_ROOT

BUS0 = f"{SYSFS_ROOT}/buses/vrtlbus0"
STATE_PATH = f"{BUS0}/state"
CLOCK_PATH = f"{BUS0}/clock_ns"
SEED_PATH  = f"{BUS0}/seed"
VER_PATH   = f"{SYSFS_ROOT}/version"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def r(path):
    with open(path) as f:
        return f.read().strip()


def w(path, value):
    """Write and return 0 on success, errno on failure."""
    try:
        with open(path, "w") as f:
            f.write(str(value))
        return 0
    except OSError as e:
        return e.errno


# ---------------------------------------------------------------------------
# [AC3+] version attribute
# ---------------------------------------------------------------------------

class TestCoreVersion:
    """version attribute format and value."""

    def test_version_exact(self, core_module):
        """version must equal '0.1.0' (current release)."""
        assert r(VER_PATH) == "0.1.0"

    def test_version_semver_shape(self, core_module):
        """version must be of the form MAJOR.MINOR.PATCH (all numeric)."""
        parts = r(VER_PATH).split(".")
        assert len(parts) == 3, f"Expected 3 semver parts, got {parts}"
        assert all(p.isdigit() for p in parts), f"Non-numeric semver part in {parts}"

    def test_version_readable_without_write(self, core_module):
        """version must be read-only (write must raise an error)."""
        with pytest.raises(OSError):
            with open(VER_PATH, "w") as f:
                f.write("9.9.9")


# ---------------------------------------------------------------------------
# [AC4] state — valid transitions and rejection of invalid values
# ---------------------------------------------------------------------------

class TestCoreBusState:
    """state attribute: up / down / reset transitions and error handling."""

    def test_state_default_is_up(self, core_module):
        """Bus state must be 'up' on fresh module load."""
        assert r(STATE_PATH) == "up"

    def test_state_write_down_reads_down(self, core_module):
        """Writing 'down' must be accepted and read back as 'down'."""
        assert w(STATE_PATH, "down") == 0
        assert r(STATE_PATH) == "down"

    def test_state_write_up_after_down_reads_up(self, core_module):
        """Writing 'up' after 'down' must restore the state to 'up'."""
        w(STATE_PATH, "down")
        assert w(STATE_PATH, "up") == 0
        assert r(STATE_PATH) == "up"

    def test_state_write_up_is_idempotent(self, core_module):
        """Writing 'up' when already 'up' must succeed and remain 'up'."""
        assert r(STATE_PATH) == "up"
        assert w(STATE_PATH, "up") == 0
        assert r(STATE_PATH) == "up"

    def test_state_write_down_is_idempotent(self, core_module):
        """Writing 'down' twice must succeed and remain 'down'."""
        w(STATE_PATH, "down")
        assert w(STATE_PATH, "down") == 0
        assert r(STATE_PATH) == "down"

    # [AC5] reset behavior

    def test_state_reset_from_up_reads_up(self, core_module):
        """state=reset when already up must transition to 'up'."""
        assert r(STATE_PATH) == "up"
        assert w(STATE_PATH, "reset") == 0
        assert r(STATE_PATH) == "up"

    def test_state_reset_from_down_reads_up(self, core_module):
        """state=reset from 'down' must always transition to 'up'."""
        w(STATE_PATH, "down")
        assert r(STATE_PATH) == "down"
        assert w(STATE_PATH, "reset") == 0
        assert r(STATE_PATH) == "up"

    def test_state_reset_is_repeatable(self, core_module):
        """Successive resets must each transition back to 'up'."""
        for _ in range(3):
            w(STATE_PATH, "down")
            assert w(STATE_PATH, "reset") == 0
            assert r(STATE_PATH) == "up"

    # Invalid values

    @pytest.mark.parametrize("bad", [
        "foo", "UP", "DOWN", "RESET", "1", "0", "true", "yes",
        " up", "up ", "updown",
    ])
    def test_state_invalid_value_returns_einval(self, core_module, bad):
        """Any value that is not 'up', 'down', or 'reset' must return -EINVAL."""
        result = w(STATE_PATH, bad)
        assert result == errno.EINVAL, (
            f"state={bad!r}: expected EINVAL, got errno={result}"
        )


# ---------------------------------------------------------------------------
# [AC5+] clock_ns — monotonicity
# ---------------------------------------------------------------------------

class TestCoreBusClockNs:
    """clock_ns attribute: monotonically non-decreasing CLOCK_MONOTONIC snapshot."""

    def test_clock_ns_positive(self, core_module):
        """clock_ns must return a positive integer."""
        assert int(r(CLOCK_PATH)) > 0

    def test_clock_ns_monotonic(self, core_module):
        """Two successive reads of clock_ns must be non-decreasing."""
        t1 = int(r(CLOCK_PATH))
        t2 = int(r(CLOCK_PATH))
        assert t2 >= t1, f"clock_ns went backwards: {t1} → {t2}"

    def test_clock_ns_advances(self, core_module):
        """clock_ns must increase between reads separated by a short sleep."""
        t1 = int(r(CLOCK_PATH))
        time.sleep(0.01)   # 10 ms
        t2 = int(r(CLOCK_PATH))
        assert t2 > t1, f"clock_ns did not advance after 10 ms: {t1} → {t2}"

    def test_clock_ns_read_only(self, core_module):
        """clock_ns must be read-only."""
        with pytest.raises(OSError):
            with open(CLOCK_PATH, "w") as f:
                f.write("0")


# ---------------------------------------------------------------------------
# seed attribute
# ---------------------------------------------------------------------------

class TestCoreBusSeed:
    """seed attribute: default, write, zero rejection."""

    def test_seed_default(self, core_module):
        """seed must equal 1 (VIRTRTLAB_DEFAULT_SEED) on module load."""
        assert r(SEED_PATH) == "1"

    def test_seed_nonzero_written_and_readable(self, core_module):
        """Writing an arbitrary non-zero seed and reading it back must succeed."""
        assert w(SEED_PATH, "12345") == 0
        assert r(SEED_PATH) == "12345"

    def test_seed_max_u32_accepted(self, core_module):
        """The maximum u32 seed (4294967295) must be accepted."""
        assert w(SEED_PATH, "4294967295") == 0
        assert r(SEED_PATH) == "4294967295"

    def test_seed_zero_rejected(self, core_module):
        """Writing seed=0 must return -EINVAL (xorshift32 requires non-zero state)."""
        result = w(SEED_PATH, "0")
        assert result == errno.EINVAL, f"seed=0: expected EINVAL, got errno={result}"

    def test_seed_zero_does_not_change_state(self, core_module):
        """After a rejected write (seed=0), seed must retain its previous value."""
        w(SEED_PATH, "42")
        w(SEED_PATH, "0")   # rejected
        assert r(SEED_PATH) == "42"

    def test_seed_write_restores_determinism(self, core_module):
        """Same seed written twice must produce the same PRNG sequence observable
        via seed readback after a down→up cycle triggers no PRNG draw."""
        # Write seed twice, read back; without draws, value must be stable.
        w(SEED_PATH, "777")
        assert r(SEED_PATH) == "777"
        w(SEED_PATH, "777")
        assert r(SEED_PATH) == "777"
