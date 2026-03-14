"""
test_core_load.py — load/unload tests for virtrtlab_core.

Validates:
  - module loads without error
  - expected sysfs tree is created
  - expected dmesg message appears on load
  - sysfs tree is removed after unload
  - expected dmesg message appears on unload
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

# ---------------------------------------------------------------------------
# Expected sysfs paths created by virtrtlab_core
# ---------------------------------------------------------------------------

CORE_SYSFS_PATHS = [
    SYSFS_ROOT,
    f"{SYSFS_ROOT}/version",
    f"{SYSFS_ROOT}/buses",
    f"{SYSFS_ROOT}/buses/vrtlbus0",
    f"{SYSFS_ROOT}/buses/vrtlbus0/state",
    f"{SYSFS_ROOT}/buses/vrtlbus0/clock_ns",
    f"{SYSFS_ROOT}/buses/vrtlbus0/seed",
    f"{SYSFS_ROOT}/devices",
]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestCoreLoad:
    """virtrtlab_core load / sysfs presence."""

    def test_module_loads(self, core_module):
        """insmod succeeds and module appears in /proc/modules."""
        assert _module_loaded("virtrtlab_core")

    def test_dmesg_load_message(self):
        """dmesg must contain the 'loaded' banner emitted by pr_info."""
        # Capture a dmesg baseline before loading the module to avoid
        # matching banners from earlier tests/runs.
        baseline = dmesg_lines()
        baseline_len = len(baseline)

        _insmod(KO["core"])
        try:
            lines = dmesg_lines()
            # If dmesg_lines() returns a sliding window (e.g. last 200 lines),
            # baseline_len may be greater than the current length. In that case,
            # fall back to searching the full window.
            if baseline_len < len(lines):
                new_lines = lines[baseline_len:]
            else:
                new_lines = lines

            matching = [
                l for l in new_lines if "virtrtlab_core" in l and "loaded" in l
            ]
            assert matching, (
                "Expected 'virtrtlab_core: loaded' in dmesg after insmod, got:\n"
                + "\n".join(new_lines[-20:])
            )
        finally:
            _rmmod("virtrtlab_core")

    @pytest.mark.parametrize("path", CORE_SYSFS_PATHS)
    def test_sysfs_path_exists(self, core_module, path):
        """Every expected sysfs entry must exist after module load."""
        assert os.path.exists(path), f"Missing sysfs path: {path}"

    def test_sysfs_version_content(self, core_module):
        """version attribute must return a non-empty string ending with newline."""
        with open(f"{SYSFS_ROOT}/version") as f:
            content = f.read()
        assert content.strip(), "version attribute is empty"
        # Basic semver shape: at least one dot
        assert "." in content.strip(), f"Unexpected version format: {content!r}"

    def test_sysfs_bus_state_default(self, core_module):
        """Bus state must be 'up' immediately after load."""
        with open(f"{SYSFS_ROOT}/buses/vrtlbus0/state") as f:
            state = f.read().strip()
        assert state == "up", f"Expected state='up', got {state!r}"

    def test_sysfs_clock_ns_readable(self, core_module):
        """clock_ns must return a positive integer."""
        with open(f"{SYSFS_ROOT}/buses/vrtlbus0/clock_ns") as f:
            value = int(f.read().strip())
        assert value > 0, "clock_ns must be a positive integer"


class TestCoreUnload:
    """virtrtlab_core unload / sysfs cleanup."""

    def test_module_unloads(self):
        """rmmod succeeds and module disappears from /proc/modules."""
        if not os.path.exists(KO["core"]):
            pytest.skip(f"Module not built: {KO['core']}")
        # Load then unload manually so we can assert the unload dmesg
        _rmmod("virtrtlab_core")
        _insmod(KO["core"])
        assert _module_loaded("virtrtlab_core")
        _rmmod("virtrtlab_core")
        assert not _module_loaded("virtrtlab_core")

    def test_dmesg_unload_message(self):
        """dmesg must contain 'unloaded' after rmmod."""
        if not os.path.exists(KO["core"]):
            pytest.skip(f"Module not built: {KO['core']}")
        _rmmod("virtrtlab_core")
        _insmod(KO["core"])
        _rmmod("virtrtlab_core")
        lines = dmesg_lines()
        matching = [l for l in lines if "virtrtlab_core" in l and "unloaded" in l]
        assert matching, (
            "Expected 'virtrtlab_core: unloaded' in dmesg after rmmod, got:\n"
            + "\n".join(lines[-20:])
        )

    def test_sysfs_removed_after_unload(self):
        """sysfs tree must be gone after rmmod."""
        if not os.path.exists(KO["core"]):
            pytest.skip(f"Module not built: {KO['core']}")
        _rmmod("virtrtlab_core")
        _insmod(KO["core"])
        assert os.path.exists(SYSFS_ROOT)
        _rmmod("virtrtlab_core")
        assert not os.path.exists(SYSFS_ROOT), (
            f"{SYSFS_ROOT} still present after rmmod"
        )
