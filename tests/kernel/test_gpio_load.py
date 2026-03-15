"""
test_gpio_load.py — load/unload tests for virtrtlab_gpio.

Validates:
  - module loads with default param (1 device)
  - expected sysfs tree for gpio0 is created
  - dmesg messages appear on load and unload
  - sysfs tree is removed after unload
  - module loads with num_gpio_devs=2 and creates gpio0 + gpio1
  - invalid num_gpio_devs value is rejected (module refuses to load)
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
    "num_lines",
    "chip_path",
    "inject",
    "enabled",
    "latency_ns",
    "jitter_ns",
    "drop_rate_ppm",
    "bitflip_rate_ppm",
]

GPIO0_STATS_ATTRS = [
    "stats/value_changes",
    "stats/drops",
    "stats/bitflips",
    "stats/reset",
]


def _gpio_path(bank, attr=""):
    base = f"{DEVICES_ROOT}/gpio{bank}"
    return os.path.join(base, attr) if attr else base


# ---------------------------------------------------------------------------
# Tests — default load (1 bank)
# ---------------------------------------------------------------------------

class TestGpioLoadDefault:
    """virtrtlab_gpio default load: 1 device."""

    def test_module_loads(self, gpio_module):
        """insmod succeeds and module appears in /proc/modules."""
        assert _module_loaded("virtrtlab_gpio")

    def test_dmesg_load_message(self):
        """dmesg must contain 'gpio0 registered' after insmod."""
        # Ensure a clean module state for this test.
        if _module_loaded("virtrtlab_gpio"):
            _rmmod("virtrtlab_gpio")

        # core must be present as a pre-requisite
        core_was_loaded = _module_loaded("virtrtlab_core")
        if not core_was_loaded:
            _insmod(KO["core"])

        # Clear the kernel ring buffer so we only see messages from this insmod.
        subprocess.run(["sudo", "dmesg", "-C"], check=True)

        try:
            _insmod(KO["gpio"])
            lines = dmesg_lines()
            matching = [l for l in lines
                        if ("virtrtlab_gpio" in l and
                            "gpio0" in l and
                            "registered" in l)]
            assert matching, (
                "Expected 'gpio0 registered on virtrtlab bus' in dmesg, got:\n"
                + "\n".join(lines[-20:])
            )
        finally:
            if _module_loaded("virtrtlab_gpio"):
                _rmmod("virtrtlab_gpio")
            if not core_was_loaded:
                _rmmod("virtrtlab_core")

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

    def test_sysfs_gpio0_num_lines(self, gpio_module):
        """num_lines must read back '8'."""
        with open(_gpio_path(0, "num_lines")) as f:
            assert f.read().strip() == "8"

    def test_sysfs_gpio0_chip_path(self, gpio_module):
        """chip_path must be a non-empty /dev/gpiochipN path that exists."""
        with open(_gpio_path(0, "chip_path")) as f:
            path = f.read().strip()
        assert path.startswith("/dev/gpiochip"), (
            f"chip_path does not look like a gpiochip device: {path!r}"
        )
        assert os.path.exists(path), (
            f"chip_path '{path}' does not exist in /dev/"
        )

    @pytest.mark.parametrize("stat", ["value_changes", "drops", "bitflips"])
    def test_sysfs_gpio0_stats_default(self, gpio_module, stat):
        """All stats counters must default to 0."""
        with open(_gpio_path(0, f"stats/{stat}")) as f:
            assert f.read().strip() == "0"

    def test_no_gpio1_with_single_device(self, gpio_module):
        """gpio1 must NOT exist when num_gpio_devs=1."""
        assert not os.path.exists(_gpio_path(1)), \
            "gpio1 dir unexpectedly present with num_gpio_devs=1"


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

class TestGpioMultiDev:
    """virtrtlab_gpio with num_gpio_devs=2."""

    def _load_two_devs(self):
        _rmmod("virtrtlab_gpio")
        _rmmod("virtrtlab_core")
        _insmod(KO["core"])
        _insmod(KO["gpio"], "num_gpio_devs=2")

    def _unload_all(self):
        _rmmod("virtrtlab_gpio")
        _rmmod("virtrtlab_core")

    def test_two_devs_load(self):
        """Both gpio0 and gpio1 dirs must exist with num_gpio_devs=2."""
        for ko in ("core", "gpio"):
            if not os.path.exists(KO[ko]):
                pytest.skip(f"Module not built: {KO[ko]}")
        self._load_two_devs()
        try:
            assert os.path.isdir(_gpio_path(0)), "gpio0 dir missing"
            assert os.path.isdir(_gpio_path(1)), "gpio1 dir missing"
            assert not os.path.exists(_gpio_path(2)), \
                "gpio2 unexpectedly present with num_gpio_devs=2"
        finally:
            self._unload_all()

    def test_two_devs_dmesg(self):
        """dmesg must contain registration messages for both devices."""
        for ko in ("core", "gpio"):
            if not os.path.exists(KO[ko]):
                pytest.skip(f"Module not built: {KO[ko]}")
        self._load_two_devs()
        try:
            lines = dmesg_lines()
            for dev in (0, 1):
                matching = [l for l in lines
                            if "virtrtlab_gpio" in l
                            and f"gpio{dev}" in l
                            and "registered" in l]
                assert matching, f"Missing dmesg registration for gpio{dev}"
        finally:
            self._unload_all()

    def test_two_devs_unload_dmesg(self):
        """dmesg must contain unregistration messages for both devices after rmmod."""
        for ko in ("core", "gpio"):
            if not os.path.exists(KO[ko]):
                pytest.skip(f"Module not built: {KO[ko]}")
        self._load_two_devs()
        self._unload_all()
        lines = dmesg_lines()
        for dev in (0, 1):
            matching = [l for l in lines
                        if "virtrtlab_gpio" in l
                        and f"gpio{dev}" in l
                        and "unregistered" in l]
            assert matching, f"Missing dmesg unregistration for gpio{dev}"


# ---------------------------------------------------------------------------
# Tests — invalid module parameter
# ---------------------------------------------------------------------------

class TestGpioInvalidParam:
    """virtrtlab_gpio refuses invalid num_gpio_devs values."""

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
            # Successful load: insmod should have returned success.
            assert result.returncode == 0, (
                "virtrtlab_gpio loaded but insmod returned non-zero: "
                f"rc={result.returncode}, stderr={result.stderr!r}"
            )
            _rmmod("virtrtlab_gpio")
        else:
            # Expected path for invalid parameters: insmod must fail, and
            # stderr should indicate an invalid argument / -EINVAL.
            assert result.returncode != 0, (
                "virtrtlab_gpio did not load, but insmod returned zero; "
                "this suggests a non-parameter-related failure was not "
                "properly reported."
            )
            stderr_lower = (result.stderr or "").lower()
            assert (
                "invalid" in stderr_lower
                or "e-inval" in stderr_lower
                or "invalid argument" in stderr_lower
                or "einval" in stderr_lower
            ), (
                "insmod failed but stderr does not clearly indicate an "
                f"invalid parameter / EINVAL: {result.stderr!r}"
            )
        return loaded

    @pytest.mark.parametrize("bad_param", [
        "num_gpio_devs=0",
        "num_gpio_devs=33",
    ])
    def test_rejects_out_of_range(self, bad_param):
        """Module must refuse to load with out-of-range num_gpio_devs."""
        for ko in ("core", "gpio"):
            if not os.path.exists(KO[ko]):
                pytest.skip(f"Module not built: {KO[ko]}")
        loaded = self._try_load(bad_param)
        assert not loaded, \
            f"virtrtlab_gpio loaded with invalid param '{bad_param}' — expected rejection"
        _rmmod("virtrtlab_core")
