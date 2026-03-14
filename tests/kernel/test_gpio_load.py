"""
test_gpio_load.py — load/unload tests for virtrtlab_gpio.

Validates:
  - module loads with default param (1 bank)
  - expected sysfs tree for gpio0 is created
  - dmesg messages appear on load and unload
  - sysfs tree is removed after unload
  - module loads with num_gpio_banks=2 and creates gpio0 + gpio1
  - invalid num_gpio_banks value is rejected (module refuses to load)
"""

import os
import subprocess

import pytest

from conftest import (
    KO,
    SYSFS_ROOT,
    _insmod,
    _module_loaded,
    _rmmod,
    dmesg_lines,
)

DEVICES_ROOT = f"{SYSFS_ROOT}/devices"

# ---------------------------------------------------------------------------
# Expected sysfs paths for a single gpio bank (gpio0)
# ---------------------------------------------------------------------------

GPIO0_ATTRS = [
    "type",
    "bus",
    "enabled",
    "latency_ns",
    "jitter_ns",
    "drop_rate_ppm",
    "bitflip_rate_ppm",
    "direction",
    "value",
    "active_low",
    "edge_rising",
    "edge_falling",
]

GPIO0_STATS_ATTRS = [
    "stats/value_changes",
    "stats/edge_events",
    "stats/drops",
    "stats/reset",
]


def _gpio_path(bank, attr=""):
    base = f"{DEVICES_ROOT}/gpio{bank}"
    return os.path.join(base, attr) if attr else base


# ---------------------------------------------------------------------------
# Tests — default load (1 bank)
# ---------------------------------------------------------------------------

class TestGpioLoadDefault:
    """virtrtlab_gpio default load: 1 bank."""

    def test_module_loads(self, gpio_module):
        """insmod succeeds and module appears in /proc/modules."""
        assert _module_loaded("virtrtlab_gpio")

    def test_dmesg_load_message(self, gpio_module):
        """dmesg must contain 'gpio0 registered' after insmod."""
        lines = dmesg_lines()
        matching = [l for l in lines
                    if "virtrtlab_gpio" in l and "gpio0" in l and "registered" in l]
        assert matching, (
            "Expected 'gpio0 registered on virtrtlab bus' in dmesg, got:\n"
            + "\n".join(lines[-20:])
        )

    def test_sysfs_gpio0_dir_exists(self, gpio_module):
        """gpio0 device directory must exist under sysfs/devices/."""
        assert os.path.isdir(_gpio_path(0)), f"Missing dir: {_gpio_path(0)}"

    @pytest.mark.parametrize("attr", GPIO0_ATTRS)
    def test_sysfs_gpio0_attr_exists(self, gpio_module, attr):
        """Every expected gpio0 attribute must be present."""
        p = _gpio_path(0, attr)
        assert os.path.exists(p), f"Missing sysfs attr: {p}"

    @pytest.mark.parametrize("attr", GPIO0_STATS_ATTRS)
    def test_sysfs_gpio0_stats_attr_exists(self, gpio_module, attr):
        """Every expected gpio0/stats/* attribute must be present."""
        p = _gpio_path(0, attr)
        assert os.path.exists(p), f"Missing sysfs stats attr: {p}"

    def test_sysfs_gpio0_type(self, gpio_module):
        """type attribute must read back 'gpio'."""
        with open(_gpio_path(0, "type")) as f:
            assert f.read().strip() == "gpio"

    def test_sysfs_gpio0_bus(self, gpio_module):
        """bus attribute must read back 'vrtlbus0'."""
        with open(_gpio_path(0, "bus")) as f:
            assert f.read().strip() == "vrtlbus0"

    def test_sysfs_gpio0_enabled_default(self, gpio_module):
        """enabled must default to 1."""
        with open(_gpio_path(0, "enabled")) as f:
            assert f.read().strip() == "1"

    @pytest.mark.parametrize("attr,expected", [
        ("direction",    "0x00"),
        ("value",        "0x00"),
        ("active_low",   "0x00"),
        ("edge_rising",  "0x00"),
        ("edge_falling", "0x00"),
    ])
    def test_sysfs_gpio0_mask_defaults(self, gpio_module, attr, expected):
        """All mask attributes must default to 0x00."""
        with open(_gpio_path(0, attr)) as f:
            assert f.read().strip() == expected, \
                f"{attr} default mismatch"

    @pytest.mark.parametrize("stat", ["value_changes", "edge_events", "drops"])
    def test_sysfs_gpio0_stats_default(self, gpio_module, stat):
        """All stats counters must default to 0."""
        with open(_gpio_path(0, f"stats/{stat}")) as f:
            assert f.read().strip() == "0"

    def test_no_gpio1_with_single_bank(self, gpio_module):
        """gpio1 must NOT exist when num_gpio_banks=1."""
        assert not os.path.exists(_gpio_path(1)), \
            "gpio1 dir unexpectedly present with num_gpio_banks=1"


# ---------------------------------------------------------------------------
# Tests — unload
# ---------------------------------------------------------------------------

class TestGpioUnload:
    """virtrtlab_gpio unload / sysfs cleanup."""

    def _load_gpio(self, params=""):
        _rmmod("virtrtlab_gpio")
        _rmmod("virtrtlab_core")
        _insmod(KO["core"])
        _insmod(KO["gpio"], params)

    def _unload_all(self):
        _rmmod("virtrtlab_gpio")
        _rmmod("virtrtlab_core")

    def test_module_unloads(self):
        """rmmod succeeds and module disappears from /proc/modules."""
        if not os.path.exists(KO["gpio"]):
            pytest.skip(f"Module not built: {KO['gpio']}")
        self._load_gpio()
        assert _module_loaded("virtrtlab_gpio")
        _rmmod("virtrtlab_gpio")
        assert not _module_loaded("virtrtlab_gpio")
        _rmmod("virtrtlab_core")

    def test_dmesg_unload_message(self):
        """dmesg must contain 'gpio0 unregistered' after rmmod."""
        if not os.path.exists(KO["gpio"]):
            pytest.skip(f"Module not built: {KO['gpio']}")
        self._load_gpio()
        _rmmod("virtrtlab_gpio")
        _rmmod("virtrtlab_core")
        lines = dmesg_lines()
        matching = [l for l in lines
                    if "virtrtlab_gpio" in l and "gpio0" in l and "unregistered" in l]
        assert matching, (
            "Expected 'gpio0 unregistered' in dmesg after rmmod, got:\n"
            + "\n".join(lines[-20:])
        )

    def test_sysfs_removed_after_unload(self):
        """gpio0 sysfs dir must be gone after rmmod."""
        if not os.path.exists(KO["gpio"]):
            pytest.skip(f"Module not built: {KO['gpio']}")
        self._load_gpio()
        assert os.path.isdir(_gpio_path(0))
        self._unload_all()
        assert not os.path.exists(_gpio_path(0)), \
            f"{_gpio_path(0)} still present after rmmod"


# ---------------------------------------------------------------------------
# Tests — multi-bank load
# ---------------------------------------------------------------------------

class TestGpioMultiBank:
    """virtrtlab_gpio with num_gpio_banks=2."""

    def _load_two_banks(self):
        _rmmod("virtrtlab_gpio")
        _rmmod("virtrtlab_core")
        _insmod(KO["core"])
        _insmod(KO["gpio"], "num_gpio_banks=2")

    def _unload_all(self):
        _rmmod("virtrtlab_gpio")
        _rmmod("virtrtlab_core")

    def test_two_banks_load(self):
        """Both gpio0 and gpio1 dirs must exist with num_gpio_banks=2."""
        for ko in ("core", "gpio"):
            if not os.path.exists(KO[ko]):
                pytest.skip(f"Module not built: {KO[ko]}")
        self._load_two_banks()
        try:
            assert os.path.isdir(_gpio_path(0)), "gpio0 dir missing"
            assert os.path.isdir(_gpio_path(1)), "gpio1 dir missing"
            assert not os.path.exists(_gpio_path(2)), \
                "gpio2 unexpectedly present with num_gpio_banks=2"
        finally:
            self._unload_all()

    def test_two_banks_dmesg(self):
        """dmesg must contain registration messages for both banks."""
        for ko in ("core", "gpio"):
            if not os.path.exists(KO[ko]):
                pytest.skip(f"Module not built: {KO[ko]}")
        self._load_two_banks()
        try:
            lines = dmesg_lines()
            for bank in (0, 1):
                matching = [l for l in lines
                            if "virtrtlab_gpio" in l
                            and f"gpio{bank}" in l
                            and "registered" in l]
                assert matching, f"Missing dmesg registration for gpio{bank}"
        finally:
            self._unload_all()

    def test_two_banks_unload_dmesg(self):
        """dmesg must contain unregistration messages for both banks after rmmod."""
        for ko in ("core", "gpio"):
            if not os.path.exists(KO[ko]):
                pytest.skip(f"Module not built: {KO[ko]}")
        self._load_two_banks()
        self._unload_all()
        lines = dmesg_lines()
        for bank in (0, 1):
            matching = [l for l in lines
                        if "virtrtlab_gpio" in l
                        and f"gpio{bank}" in l
                        and "unregistered" in l]
            assert matching, f"Missing dmesg unregistration for gpio{bank}"


# ---------------------------------------------------------------------------
# Tests — invalid module parameter
# ---------------------------------------------------------------------------

class TestGpioInvalidParam:
    """virtrtlab_gpio refuses invalid num_gpio_banks values."""

    def _try_load(self, params):
        """Attempt insmod; return True if it loaded, False if it was rejected."""
        _rmmod("virtrtlab_gpio")
        if not _module_loaded("virtrtlab_core"):
            _insmod(KO["core"])
        result = subprocess.run(
            ["sudo", "insmod", KO["gpio"]] + params.split(),
            capture_output=True, text=True,
        )
        loaded = _module_loaded("virtrtlab_gpio")
        if loaded:
            _rmmod("virtrtlab_gpio")
        return loaded

    @pytest.mark.parametrize("bad_param", [
        "num_gpio_banks=0",
        "num_gpio_banks=33",
    ])
    def test_rejects_out_of_range(self, bad_param):
        """Module must refuse to load with out-of-range num_gpio_banks."""
        for ko in ("core", "gpio"):
            if not os.path.exists(KO[ko]):
                pytest.skip(f"Module not built: {KO[ko]}")
        loaded = self._try_load(bad_param)
        assert not loaded, \
            f"virtrtlab_gpio loaded with invalid param '{bad_param}' — expected rejection"
        _rmmod("virtrtlab_core")
