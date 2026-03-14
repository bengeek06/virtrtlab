"""
test_gpio_sysfs.py — sysfs contract tests for virtrtlab_gpio (issue #15).

Acceptance criteria covered:
  - Mask format: strict "0xNN" only; every other form returns -EINVAL
  - Read returns canonical lowercase "0xnn"
  - value write: AUT-input bits updated, AUT-output bits (direction=1) ignored
  - direction write: edge_rising/edge_falling bits for output-owned pins cleared
  - edge_rising/edge_falling write: stored masked by ~direction
  - enabled=0 gate: value write returns -EIO
  - bus state=down gate: value write returns -EIO; state=up restores it
  - latency_ns/jitter_ns > 10_000_000_000 returns -EINVAL; exact max accepted
  - drop_rate_ppm/bitflip_rate_ppm > 1_000_000 returns -EINVAL; exact max accepted
  - stats/value_changes counts individual bit transitions applied
  - stats/edge_events counts rising and falling edge matches
  - stats/drops counts sysfs writes suppressed by drop_rate_ppm
  - stats/reset write "0" clears all counters atomically
  - stats/reset write non-zero returns -EINVAL
  - stats/reset read returns permission error (write-only)
  - bus state=reset: clears fault attrs, stats, sets enabled=1;
                     preserves direction, active_low, edge masks, value
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

MASK_ATTRS = ["direction", "value", "active_low", "edge_rising", "edge_falling"]


# ---------------------------------------------------------------------------
# Sysfs I/O helpers
# ---------------------------------------------------------------------------

def g(bank, attr=""):
    """Return the sysfs path for gpio<bank>[/attr]."""
    base = f"{DEVICES_ROOT}/gpio{bank}"
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
# [AC-1] Mask format — strict "0xNN" enforcement
# ---------------------------------------------------------------------------

class TestGpioMaskFormat:
    """Strict 0xNN format is enforced on all five mask attributes."""

    INVALID = [
        "0",        # decimal
        "255",      # decimal
        "0x",       # missing digits
        "0x1",      # single digit
        "0x001",    # three digits
        "0x100",    # three digits
        "0xGG",     # non-hex chars
        " 0xff",    # leading space
        "0xff ",    # trailing space (not a newline)
        "ff",       # no prefix
        "1",        # too short
    ]

    VALID = ["0x00", "0xff", "0xFF", "0xAB", "0x01"]

    @pytest.mark.parametrize("bad_val", INVALID)
    def test_all_mask_attrs_reject_invalid(self, gpio_module, bad_val):
        """Every mask attr must reject non-0xNN input with EINVAL."""
        for attr in MASK_ATTRS:
            result = w(g(0, attr), bad_val)
            assert result == errno.EINVAL, (
                f"{attr}: expected EINVAL for {bad_val!r}, got errno={result}"
            )

    @pytest.mark.parametrize("good_val", VALID)
    def test_all_mask_attrs_accept_valid(self, gpio_module, good_val):
        """Every mask attr must accept the strict 0xNN form without error."""
        # direction must be written before value/edge attrs to avoid side-effects
        # Direction write: all input bits so we can write value freely
        w(g(0, "direction"), "0x00")
        for attr in MASK_ATTRS:
            result = w(g(0, attr), good_val)
            assert result == 0, (
                f"{attr}: expected success for {good_val!r}, got errno={result}"
            )

    @pytest.mark.parametrize("attr", MASK_ATTRS)
    def test_readback_is_canonical_lowercase(self, gpio_module, attr):
        """Read must return canonical lowercase 0xnn form."""
        w(g(0, "direction"), "0x00")        # ensure all input, edge writes go through
        w(g(0, attr), "0x5A")
        val = r(g(0, attr))
        assert val == "0x5a", f"{attr}: expected '0x5a', got {val!r}"


# ---------------------------------------------------------------------------
# [AC-2] value write semantics
# ---------------------------------------------------------------------------

class TestGpioValueWrite:
    """value sysfs write drives only AUT-input bits; output bits are ignored."""

    def test_write_updates_all_input_bits(self, gpio_module):
        """With direction=0x00 (all inputs), value write updates all 8 bits."""
        w(g(0, "direction"), "0x00")
        w(g(0, "value"), "0xab")
        assert r(g(0, "value")) == "0xab"

    def test_write_ignores_lower_nibble_output_bits(self, gpio_module):
        """Lower nibble as output (direction=0x0f): sysfs write must not touch it."""
        # direction bit=1 → AUT output; lower nibble = outputs
        w(g(0, "direction"), "0x0f")
        # write 0xff; only upper nibble (inputs) should change; lower stays 0x00
        w(g(0, "value"), "0xff")
        assert r(g(0, "value")) == "0xf0", (
            "Lower nibble (AUT output) must remain 0x00, upper nibble (AUT input) = 0xf"
        )

    def test_all_output_bits_ignore_write(self, gpio_module):
        """direction=0xff (all outputs): value write is completely ignored."""
        w(g(0, "direction"), "0xff")
        w(g(0, "value"), "0xff")
        assert r(g(0, "value")) == "0x00", (
            "No AUT-input bits — value must stay 0x00"
        )

    def test_mixed_direction_readback(self, gpio_module):
        """Upper nibble = output, lower = input: write 0xff → 0x0f."""
        w(g(0, "direction"), "0xf0")
        w(g(0, "value"), "0xff")
        assert r(g(0, "value")) == "0x0f"

    def test_successive_writes_accumulate(self, gpio_module):
        """Successive writes to different input-bit groups accumulate correctly."""
        w(g(0, "direction"), "0x00")
        w(g(0, "value"), "0x0f")
        assert r(g(0, "value")) == "0x0f"
        w(g(0, "value"), "0xf0")
        assert r(g(0, "value")) == "0xf0"

    def test_write_succeeds_on_output_bit_mix(self, gpio_module):
        """value write on a mixed bank must return 0 (sysfs always returns count)."""
        w(g(0, "direction"), "0x0f")
        result = w(g(0, "value"), "0xab")
        assert result == 0, f"Expected success, got errno={result}"


# ---------------------------------------------------------------------------
# [AC-3] direction write side-effects on edge masks
# ---------------------------------------------------------------------------

class TestGpioDirectionSideEffects:
    """direction write clears edge mask bits for newly-output pins."""

    def test_direction_all_output_clears_edge_rising(self, gpio_module):
        """Setting all bits to output must clear edge_rising to 0x00."""
        w(g(0, "direction"), "0x00")
        w(g(0, "edge_rising"), "0xff")
        assert r(g(0, "edge_rising")) == "0xff"     # pre-condition
        w(g(0, "direction"), "0xff")
        assert r(g(0, "edge_rising")) == "0x00"

    def test_direction_all_output_clears_edge_falling(self, gpio_module):
        w(g(0, "direction"), "0x00")
        w(g(0, "edge_falling"), "0xff")
        w(g(0, "direction"), "0xff")
        assert r(g(0, "edge_falling")) == "0x00"

    def test_direction_partial_preserves_input_edge_bits(self, gpio_module):
        """Only output-bound bits are cleared; input-bound bits are preserved."""
        w(g(0, "direction"), "0x00")
        w(g(0, "edge_rising"), "0xff")
        # make upper nibble output → upper nibble cleared, lower stays
        w(g(0, "direction"), "0xf0")
        assert r(g(0, "edge_rising")) == "0x0f"

    def test_edge_rising_store_masked_by_current_direction(self, gpio_module):
        """Writing edge_rising is automatically masked by ~direction."""
        w(g(0, "direction"), "0xf0")    # upper nibble = output
        w(g(0, "edge_rising"), "0xff")
        assert r(g(0, "edge_rising")) == "0x0f"

    def test_edge_falling_store_masked_by_current_direction(self, gpio_module):
        w(g(0, "direction"), "0xf0")
        w(g(0, "edge_falling"), "0xff")
        assert r(g(0, "edge_falling")) == "0x0f"


# ---------------------------------------------------------------------------
# [AC-4] enabled and bus-state gates
# ---------------------------------------------------------------------------

class TestGpioGates:
    """Both enabled=0 and bus state=down block value writes with -EIO."""

    def test_value_blocked_when_disabled(self, gpio_module):
        w(g(0, "enabled"), "0")
        result = w(g(0, "value"), "0xff")
        w(g(0, "enabled"), "1")        # restore before assert
        assert result == errno.EIO, f"Expected EIO, got errno={result}"

    def test_value_allowed_when_reenabled(self, gpio_module):
        w(g(0, "enabled"), "0")
        w(g(0, "enabled"), "1")
        result = w(g(0, "value"), "0xff")
        assert result == 0

    def test_value_blocked_when_bus_down(self, gpio_module):
        w(BUS0_STATE, "down")
        result = w(g(0, "value"), "0xff")
        w(BUS0_STATE, "up")            # restore before assert
        assert result == errno.EIO, f"Expected EIO with bus down, got errno={result}"

    def test_value_allowed_after_bus_up(self, gpio_module):
        w(BUS0_STATE, "down")
        w(BUS0_STATE, "up")
        result = w(g(0, "value"), "0xab")
        assert result == 0

    def test_enabled_write_invalid_returns_error(self, gpio_module):
        """Writing a non-boolean to enabled must fail."""
        result = w(g(0, "enabled"), "2")
        assert result != 0, "Expected error for enabled=2"


# ---------------------------------------------------------------------------
# [AC-5] Fault attribute bounds
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
# [AC-6] Stats counter semantics
# ---------------------------------------------------------------------------

class TestGpioStats:
    """stats/value_changes, stats/edge_events, stats/drops semantics."""

    def _reset(self):
        w(g(0, "stats/reset"), "0")

    def test_value_changes_counts_bit_transitions(self, gpio_module):
        """Flipping 8 bits 0→1 must yield value_changes=8."""
        w(g(0, "direction"), "0x00")
        w(g(0, "value"),     "0x00")
        self._reset()
        w(g(0, "value"), "0xff")
        assert int(r(g(0, "stats/value_changes"))) == 8

    def test_value_changes_no_increment_on_same_value(self, gpio_module):
        """Writing the same value twice must not increment value_changes."""
        w(g(0, "direction"), "0x00")
        w(g(0, "value"),     "0xab")
        self._reset()
        w(g(0, "value"), "0xab")
        assert int(r(g(0, "stats/value_changes"))) == 0

    def test_value_changes_partial_bits(self, gpio_module):
        """Only the bits that actually transition should be counted."""
        w(g(0, "direction"), "0x00")
        w(g(0, "value"),     "0x0f")   # lower nibble high
        self._reset()
        w(g(0, "value"), "0xff")       # upper nibble transitions: 4 bits
        assert int(r(g(0, "stats/value_changes"))) == 4

    def test_edge_events_rising(self, gpio_module):
        """All 8 bits 0→1 with edge_rising=0xff → edge_events=8."""
        w(g(0, "direction"),   "0x00")
        w(g(0, "value"),       "0x00")
        w(g(0, "edge_rising"), "0xff")
        self._reset()
        w(g(0, "value"), "0xff")
        assert int(r(g(0, "stats/edge_events"))) == 8

    def test_edge_events_falling(self, gpio_module):
        """All 8 bits 1→0 with edge_falling=0xff → edge_events=8."""
        w(g(0, "direction"),    "0x00")
        w(g(0, "value"),        "0xff")
        w(g(0, "edge_falling"), "0xff")
        self._reset()
        w(g(0, "value"), "0x00")
        assert int(r(g(0, "stats/edge_events"))) == 8

    def test_edge_events_only_masked_bits_counted(self, gpio_module):
        """edge_rising=0x0f: only 4 transitions counted on 8-bit flip."""
        w(g(0, "direction"),   "0x00")
        w(g(0, "value"),       "0x00")
        w(g(0, "edge_rising"), "0x0f")
        self._reset()
        w(g(0, "value"), "0xff")
        assert int(r(g(0, "stats/edge_events"))) == 4

    def test_edge_events_rising_not_counted_without_mask(self, gpio_module):
        """edge_rising=0x00: rising transitions are not counted."""
        w(g(0, "direction"),   "0x00")
        w(g(0, "value"),       "0x00")
        w(g(0, "edge_rising"), "0x00")
        self._reset()
        w(g(0, "value"), "0xff")
        assert int(r(g(0, "stats/edge_events"))) == 0

    def test_drops_increments_on_full_drop_rate(self, gpio_module):
        """drop_rate_ppm=1_000_000 (100%): every write is dropped, drops+=1."""
        w(g(0, "direction"),    "0x00")
        w(g(0, "value"),        "0x00")
        w(g(0, "drop_rate_ppm"), "1000000")
        self._reset()
        result = w(g(0, "value"), "0xff")
        assert result == 0, "sysfs write must succeed (count returned) even on drop"
        assert int(r(g(0, "stats/drops"))) == 1
        # value must be unchanged — the write was dropped
        assert r(g(0, "value")) == "0x00", "value must be unchanged after drop"

    def test_drops_not_incremented_at_zero_rate(self, gpio_module):
        """drop_rate_ppm=0: no drops."""
        w(g(0, "direction"),    "0x00")
        w(g(0, "drop_rate_ppm"), "0")
        self._reset()
        w(g(0, "value"), "0xff")
        assert int(r(g(0, "stats/drops"))) == 0

    def test_output_bits_not_counted_in_value_changes(self, gpio_module):
        """Bits owned by AUT (output) must not contribute to value_changes."""
        w(g(0, "direction"), "0xff")   # all outputs
        self._reset()
        w(g(0, "value"), "0xff")       # all ignored
        assert int(r(g(0, "stats/value_changes"))) == 0


# ---------------------------------------------------------------------------
# [AC-7] stats/reset contract
# ---------------------------------------------------------------------------

class TestGpioStatsReset:
    """stats/reset write/read contract."""

    def test_reset_clears_all_counters_atomically(self, gpio_module):
        """Write 0 to stats/reset must clear value_changes, edge_events, drops."""
        w(g(0, "direction"),   "0x00")
        w(g(0, "edge_rising"), "0xff")
        w(g(0, "value"),       "0xff")   # generates value_changes + edge_events
        # pre-condition: at least one counter is non-zero
        assert int(r(g(0, "stats/value_changes"))) > 0
        w(g(0, "stats/reset"), "0")
        assert r(g(0, "stats/value_changes")) == "0"
        assert r(g(0, "stats/edge_events"))   == "0"
        assert r(g(0, "stats/drops"))         == "0"

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
# [AC-8] bus state=reset semantics on GPIO device
# ---------------------------------------------------------------------------

class TestGpioBusReset:
    """Bus state=reset clears fault attrs and stats; preserves GPIO state."""

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
        """Bus reset must zero value_changes, edge_events, drops."""
        w(g(0, "direction"), "0x00")
        w(g(0, "value"),     "0xff")
        assert int(r(g(0, "stats/value_changes"))) > 0
        w(BUS0_STATE, "reset")
        assert r(g(0, "stats/value_changes")) == "0"
        assert r(g(0, "stats/edge_events"))   == "0"
        assert r(g(0, "stats/drops"))         == "0"

    def test_bus_reset_sets_enabled_true(self, gpio_module):
        """Bus reset must restore enabled=1 even when it was 0."""
        w(g(0, "enabled"), "0")
        assert r(g(0, "enabled")) == "0"
        w(BUS0_STATE, "reset")
        assert r(g(0, "enabled")) == "1"

    def test_bus_reset_preserves_direction(self, gpio_module):
        """Bus reset must not alter the direction mask."""
        w(g(0, "direction"), "0xa5")
        w(BUS0_STATE, "reset")
        assert r(g(0, "direction")) == "0xa5"

    def test_bus_reset_preserves_active_low(self, gpio_module):
        w(g(0, "active_low"), "0x3c")
        w(BUS0_STATE, "reset")
        assert r(g(0, "active_low")) == "0x3c"

    def test_bus_reset_preserves_value(self, gpio_module):
        """Bus reset must not change the current logical bank value."""
        w(g(0, "direction"), "0x00")
        w(g(0, "value"),     "0xc3")
        w(BUS0_STATE, "reset")
        assert r(g(0, "value")) == "0xc3"

    def test_bus_reset_preserves_edge_rising(self, gpio_module):
        w(g(0, "direction"),  "0x00")
        w(g(0, "edge_rising"), "0x55")
        w(BUS0_STATE, "reset")
        assert r(g(0, "edge_rising")) == "0x55"

    def test_bus_reset_preserves_edge_falling(self, gpio_module):
        w(g(0, "direction"),    "0x00")
        w(g(0, "edge_falling"), "0xaa")
        w(BUS0_STATE, "reset")
        assert r(g(0, "edge_falling")) == "0xaa"

    def test_bus_state_reads_up_after_reset(self, gpio_module):
        """Bus state attribute must read 'up' after state=reset."""
        w(BUS0_STATE, "reset")
        assert r(BUS0_STATE) == "up"

    def test_value_write_works_after_bus_reset_from_disabled(self, gpio_module):
        """reset sets enabled=1: value write must succeed afterwards."""
        w(g(0, "enabled"), "0")
        w(BUS0_STATE, "reset")         # restores enabled=1
        result = w(g(0, "value"), "0xab")
        assert result == 0

    def test_value_write_works_after_bus_reset_from_down(self, gpio_module):
        """After reset (which transitions to up), value write must succeed."""
        w(BUS0_STATE, "reset")
        result = w(g(0, "value"), "0x42")
        assert result == 0


# ---------------------------------------------------------------------------
# [AC-B1] active_low inversion applied by the kernel
# ---------------------------------------------------------------------------

class TestGpioActiveLow:
    """
    active_low is applied by the kernel: sysfs always exposes the logical
    domain.  Physical = logical XOR active_low is an internal detail.

    B1 regression tests — all these were broken before the fix.
    """

    def test_active_low_default(self, gpio_module):
        """active_low defaults to 0x00 (no inversion)."""
        assert r(g(0, "active_low")) == "0x00"

    def test_full_inversion_visible_in_value_readback(self, gpio_module):
        """With active_low=0xFF the kernel-internal physical=0x00 reads as logical 0xFF."""
        w(g(0, "direction"),  "0x00")
        w(g(0, "active_low"), "0xff")
        assert r(g(0, "value")) == "0xff"

    def test_write_logical_zero_reads_back_zero(self, gpio_module):
        """Write logical 0x00 with active_low=0xFF stores physical=0xFF; reads back 0x00."""
        w(g(0, "direction"),  "0x00")
        w(g(0, "active_low"), "0xff")
        w(g(0, "value"),      "0x00")
        assert r(g(0, "value")) == "0x00"

    def test_logical_round_trip(self, gpio_module):
        """Write 0xa5 with active_low=0xFF: physical=0x5a, reads back 0xa5."""
        w(g(0, "direction"),  "0x00")
        w(g(0, "active_low"), "0xff")
        w(g(0, "value"),      "0xa5")
        assert r(g(0, "value")) == "0xa5"

    def test_partial_inversion(self, gpio_module):
        """active_low=0x0F inverts low nibble: physical=0x00 reads as logical 0x0F."""
        w(g(0, "direction"),  "0x00")
        w(g(0, "active_low"), "0x0f")
        assert r(g(0, "value")) == "0x0f"

    def test_rising_edge_detected_in_logical_domain(self, gpio_module):
        """
        With active_low=0x01 on bit 0:
          - initial logical bit 0 = 1 (physical 0 XOR 1)
          - write logical 0x00 → physical=0x01 → logical 0x00 (falling; no event since edge_falling=0)
          - write logical 0x01 → physical=0x00 → logical 0x01 (rising; +1 event)
        """
        w(g(0, "direction"),   "0x00")
        w(g(0, "active_low"),  "0x01")
        w(g(0, "edge_rising"), "0x01")
        # logical 1→0: falling, not counted (edge_falling not set)
        w(g(0, "value"), "0x00")
        assert int(r(g(0, "stats/edge_events"))) == 0
        # logical 0→1: rising, counted
        w(g(0, "value"), "0x01")
        assert int(r(g(0, "stats/edge_events"))) == 1

    def test_value_changes_counts_logical_transitions(self, gpio_module):
        """Write logical 0x00 when active_low=0xFF (logical init=0xFF): 8 bit transitions."""
        w(g(0, "direction"),  "0x00")
        w(g(0, "active_low"), "0xff")
        # logical = 0xFF initially (physical=0x00, active_low=0xFF)
        w(g(0, "value"), "0x00")
        assert int(r(g(0, "stats/value_changes"))) == 8


# ---------------------------------------------------------------------------
# [AC-B2] jitter_ns values above UINT32_MAX (~4.3e9) are usable
# ---------------------------------------------------------------------------

class TestGpioLargeJitter:
    """
    B2 regression: jitter_ns in the range (UINT32_MAX, MAX_LATENCY_NS] must
    not be silently biased.  We verify acceptance and basic functionality;
    statistical distribution correctness requires many samples and is not
    tested here.
    """

    def test_jitter_above_u32_max_accepted_and_readable(self, gpio_module):
        """jitter_ns=5_000_000_000 (> UINT32_MAX ~4.3e9) is writable and reads back."""
        w(g(0, "jitter_ns"), "5000000000")
        assert r(g(0, "jitter_ns")) == "5000000000"

    def test_value_write_accepted_with_large_jitter(self, gpio_module):
        """A value write with jitter_ns > UINT32_MAX must be accepted (no EINVAL)."""
        w(g(0, "direction"),  "0x00")
        w(g(0, "latency_ns"), "1000000")    # 1 ms base
        w(g(0, "jitter_ns"),  "5000000000") # 5 s amplitude — timer fires async
        result = w(g(0, "value"), "0x01")
        assert result == 0, "value write with large jitter must succeed"
