"""
test_uart_load.py — load/unload tests for virtrtlab_uart.

Validates:
  - module loads with default param (1 device)
  - expected sysfs tree for uart0 is created
  - expected dmesg message appears on load
  - default attribute values match the spec (tty_std_termios, fault attrs = 0)
  - all stat counters read 0 immediately after load
  - sysfs tree is removed after unload
  - expected dmesg message appears on unload
  - module loads with num_uart_devices=2 and creates uart0 + uart1
  - invalid num_uart_devices values are rejected by the module
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
# Expected sysfs attribute lists
# ---------------------------------------------------------------------------

UART0_ATTRS = [
    "type",
    "bus",
    "enabled",
    "latency_ns",
    "jitter_ns",
    "drop_rate_ppm",
    "bitflip_rate_ppm",
    "baud",
    "parity",
    "databits",
    "stopbits",
    "tx_buf_sz",
    "rx_buf_sz",
]

UART0_STATS_ATTRS = [
    "stats/tx_bytes",
    "stats/rx_bytes",
    "stats/overruns",
    "stats/drops",
    "stats/reset",
]


def _uart_path(idx, attr=""):
    base = f"{DEVICES_ROOT}/uart{idx}"
    return os.path.join(base, attr) if attr else base


# ---------------------------------------------------------------------------
# Tests — default load (1 device)
# ---------------------------------------------------------------------------

class TestUartLoadDefault:
    """virtrtlab_uart default load: 1 device."""

    def test_module_loads(self, uart_module):
        """insmod succeeds and module appears in /proc/modules."""
        assert _module_loaded("virtrtlab_uart")

    def test_dmesg_load_message(self):
        """dmesg must contain 'uart0 registered' after insmod."""
        if _module_loaded("virtrtlab_uart"):
            _rmmod("virtrtlab_uart")

        core_was_loaded = _module_loaded("virtrtlab_core")
        if not core_was_loaded:
            _insmod(KO["core"])

        subprocess.run(["sudo", "dmesg", "-C"], check=True)

        try:
            _insmod(KO["uart"])
            lines = dmesg_lines()
            matching = [
                l for l in lines
                if "virtrtlab_uart" in l and "uart0" in l and "registered" in l
            ]
            assert matching, (
                "Expected 'uart0 registered on virtrtlab bus' in dmesg, got:\n"
                + "\n".join(lines[-20:])
            )
        finally:
            if _module_loaded("virtrtlab_uart"):
                _rmmod("virtrtlab_uart")
            if not core_was_loaded:
                _rmmod("virtrtlab_core")

    def test_sysfs_uart0_dir_exists(self, uart_module):
        """uart0 device directory must exist under sysfs/devices/."""
        assert os.path.isdir(_uart_path(0)), f"Missing dir: {_uart_path(0)}"

    @pytest.mark.parametrize("attr", UART0_ATTRS)
    def test_sysfs_uart0_attr_exists(self, uart_module, attr):
        """Every expected uart0 attribute must be present."""
        p = _uart_path(0, attr)
        assert os.path.exists(p), f"Missing sysfs attr: {p}"

    @pytest.mark.parametrize("attr", UART0_STATS_ATTRS)
    def test_sysfs_uart0_stats_attr_exists(self, uart_module, attr):
        """Every expected uart0/stats/* attribute must be present."""
        p = _uart_path(0, attr)
        assert os.path.exists(p), f"Missing sysfs stats attr: {p}"

    def test_sysfs_uart0_type(self, uart_module):
        """type attribute must read back 'uart'."""
        with open(_uart_path(0, "type")) as f:
            assert f.read().strip() == "uart"

    def test_sysfs_uart0_bus(self, uart_module):
        """bus attribute must read back 'vrtlbus0'."""
        with open(_uart_path(0, "bus")) as f:
            assert f.read().strip() == "vrtlbus0"

    def test_sysfs_uart0_enabled_default(self, uart_module):
        """enabled must be 1 at load time."""
        with open(_uart_path(0, "enabled")) as f:
            assert f.read().strip() == "1"

    def test_sysfs_uart0_baud_default(self, uart_module):
        """baud must reflect tty_std_termios default (38400) at load time."""
        with open(_uart_path(0, "baud")) as f:
            assert f.read().strip() == "38400"

    def test_sysfs_uart0_parity_default(self, uart_module):
        """parity must be 'none' at load time."""
        with open(_uart_path(0, "parity")) as f:
            assert f.read().strip() == "none"

    def test_sysfs_uart0_databits_default(self, uart_module):
        """databits must be 8 at load time."""
        with open(_uart_path(0, "databits")) as f:
            assert f.read().strip() == "8"

    def test_sysfs_uart0_stopbits_default(self, uart_module):
        """stopbits must be 1 at load time."""
        with open(_uart_path(0, "stopbits")) as f:
            assert f.read().strip() == "1"

    @pytest.mark.parametrize("attr,expected", [
        ("latency_ns",        "0"),
        ("jitter_ns",         "0"),
        ("drop_rate_ppm",     "0"),
        ("bitflip_rate_ppm",  "0"),
    ])
    def test_sysfs_uart0_fault_attr_defaults(self, uart_module, attr, expected):
        """All fault injection attributes must default to 0 at load time."""
        with open(_uart_path(0, attr)) as f:
            assert f.read().strip() == expected, f"{attr} must default to {expected}"

    @pytest.mark.parametrize("counter", ["tx_bytes", "rx_bytes", "overruns", "drops"])
    def test_sysfs_stats_all_zero_after_load(self, uart_module, counter):
        """All stat counters must read 0 immediately after module load (issue #3)."""
        with open(_uart_path(0, f"stats/{counter}")) as f:
            assert f.read().strip() == "0", f"Expected 0 for stats/{counter}"

    def test_no_uart1_with_single_device(self, uart_module):
        """uart1 must NOT exist when num_uart_devices=1."""
        assert not os.path.exists(_uart_path(1)), \
            "uart1 dir unexpectedly present with num_uart_devices=1"


# ---------------------------------------------------------------------------
# Tests — unload
# ---------------------------------------------------------------------------

class TestUartUnload:
    """virtrtlab_uart unload / sysfs cleanup."""

    def _load_uart(self, params=""):
        _rmmod("virtrtlab_uart")
        _rmmod("virtrtlab_core")
        _insmod(KO["core"])
        _insmod(KO["uart"], params)

    def _unload_all(self):
        _rmmod("virtrtlab_uart")
        _rmmod("virtrtlab_core")

    def test_module_unloads(self):
        """rmmod succeeds and module disappears from /proc/modules."""
        for ko in ("core", "uart"):
            if not os.path.exists(KO[ko]):
                pytest.skip(f"Module not built: {KO[ko]}")
        self._load_uart()
        assert _module_loaded("virtrtlab_uart")
        _rmmod("virtrtlab_uart")
        assert not _module_loaded("virtrtlab_uart")
        _rmmod("virtrtlab_core")

    def test_dmesg_unload_message(self):
        """dmesg must contain 'uart0 unregistered' after rmmod."""
        for ko in ("core", "uart"):
            if not os.path.exists(KO[ko]):
                pytest.skip(f"Module not built: {KO[ko]}")
        self._load_uart()
        _rmmod("virtrtlab_uart")
        _rmmod("virtrtlab_core")
        lines = dmesg_lines()
        matching = [
            l for l in lines
            if "virtrtlab_uart" in l and "uart0" in l and "unregistered" in l
        ]
        assert matching, (
            "Expected 'uart0 unregistered' in dmesg after rmmod, got:\n"
            + "\n".join(lines[-20:])
        )

    def test_sysfs_removed_after_unload(self):
        """uart0 sysfs directory must be gone after rmmod."""
        for ko in ("core", "uart"):
            if not os.path.exists(KO[ko]):
                pytest.skip(f"Module not built: {KO[ko]}")
        self._load_uart()
        assert os.path.isdir(_uart_path(0))
        self._unload_all()
        assert not os.path.exists(_uart_path(0)), \
            f"{_uart_path(0)} still present after rmmod"


# ---------------------------------------------------------------------------
# Tests — multi-device load
# ---------------------------------------------------------------------------

class TestUartLoadMultiDevice:
    """virtrtlab_uart with num_uart_devices=2."""

    def _load_two_devices(self):
        _rmmod("virtrtlab_uart")
        _rmmod("virtrtlab_core")
        _insmod(KO["core"])
        _insmod(KO["uart"], "num_uart_devices=2")

    def _unload_all(self):
        _rmmod("virtrtlab_uart")
        _rmmod("virtrtlab_core")

    def test_two_devices_appear(self):
        """uart0 and uart1 dirs must both exist with num_uart_devices=2."""
        for ko in ("core", "uart"):
            if not os.path.exists(KO[ko]):
                pytest.skip(f"Module not built: {KO[ko]}")
        self._load_two_devices()
        try:
            assert os.path.isdir(_uart_path(0)), "uart0 dir missing"
            assert os.path.isdir(_uart_path(1)), "uart1 dir missing"
            assert not os.path.exists(_uart_path(2)), \
                "uart2 unexpectedly present with num_uart_devices=2"
        finally:
            self._unload_all()

    def test_two_devices_dmesg(self):
        """dmesg must contain registration messages for both uart0 and uart1."""
        for ko in ("core", "uart"):
            if not os.path.exists(KO[ko]):
                pytest.skip(f"Module not built: {KO[ko]}")
        subprocess.run(["sudo", "dmesg", "-C"], check=True)
        self._load_two_devices()
        try:
            lines = dmesg_lines()
            for idx in (0, 1):
                matching = [
                    l for l in lines
                    if "virtrtlab_uart" in l
                    and f"uart{idx}" in l
                    and "registered" in l
                ]
                assert matching, f"Expected 'uart{idx} registered' in dmesg"
        finally:
            self._unload_all()

    @pytest.mark.parametrize("attr", UART0_ATTRS)
    def test_uart1_attrs_exist(self, attr):
        """Every expected attribute must also be present on uart1."""
        for ko in ("core", "uart"):
            if not os.path.exists(KO[ko]):
                pytest.skip(f"Module not built: {KO[ko]}")
        self._load_two_devices()
        try:
            p = _uart_path(1, attr)
            assert os.path.exists(p), f"Missing sysfs attr on uart1: {p}"
        finally:
            self._unload_all()


# ---------------------------------------------------------------------------
# Tests — invalid module parameter
# ---------------------------------------------------------------------------

class TestUartLoadInvalidParam:
    """num_uart_devices bounds validation."""

    def _ensure_core(self):
        if not _module_loaded("virtrtlab_core"):
            _insmod(KO["core"])

    def test_zero_devices_rejected(self):
        """num_uart_devices=0 must cause insmod to fail."""
        for ko in ("core", "uart"):
            if not os.path.exists(KO[ko]):
                pytest.skip(f"Module not built: {KO[ko]}")
        self._ensure_core()
        try:
            result = subprocess.run(
                ["sudo", "insmod", KO["uart"], "num_uart_devices=0"],
                capture_output=True, text=True,
            )
            assert result.returncode != 0, \
                "Expected insmod to fail with num_uart_devices=0"
        finally:
            _rmmod("virtrtlab_uart")

    def test_too_many_devices_rejected(self):
        """num_uart_devices=5 must cause insmod to fail (max is 4)."""
        for ko in ("core", "uart"):
            if not os.path.exists(KO[ko]):
                pytest.skip(f"Module not built: {KO[ko]}")
        self._ensure_core()
        try:
            result = subprocess.run(
                ["sudo", "insmod", KO["uart"], "num_uart_devices=5"],
                capture_output=True, text=True,
            )
            assert result.returncode != 0, \
                "Expected insmod to fail with num_uart_devices=5"
        finally:
            _rmmod("virtrtlab_uart")
