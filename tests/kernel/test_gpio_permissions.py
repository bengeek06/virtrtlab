# SPDX-License-Identifier: MIT

"""
test_gpio_permissions.py — GPIO device permission tests (issue #48).

Verifies that after loading virtrtlab_gpio:
  - /dev/gpiochipN is crw-rw---- (0660) and owned by root:virtrtlab
  - /sys/.../gpio0/inject is group-writable (g+w)

These permissions are enforced by udev rules in install/90-virtrtlab.rules
and must hold immediately after modprobe without any CLI intervention.

Acceptance criteria covered:
  - gpiochip character device mode is 0660
  - gpiochip group is 'virtrtlab'
  - inject sysfs attribute has group-write bit set
"""

import grp
import os
import stat

import pytest

from conftest import (
    KO,
    SYSFS_ROOT,
    _module_loaded,
)

DEVICES_ROOT    = f"{SYSFS_ROOT}/devices"
VIRTRTLAB_GROUP = "virtrtlab"


def _virtrtlab_gid():
    """Return the GID of 'virtrtlab', or None if the group does not exist."""
    try:
        return grp.getgrnam(VIRTRTLAB_GROUP).gr_gid
    except KeyError:
        return None


def _chip_path():
    """
    Read /sys/kernel/virtrtlab/devices/gpio0/chip_path and return the
    /dev/gpiochipN string, or skip the test if it is not available.
    """
    attr = f"{DEVICES_ROOT}/gpio0/chip_path"
    if not os.path.exists(attr):
        pytest.skip(f"sysfs attribute not found: {attr}")
    return open(attr).read().strip()


# ---------------------------------------------------------------------------
# /dev/gpiochipN permissions (udev MODE/GROUP rule)
# ---------------------------------------------------------------------------

class TestGpioChipPermissions:
    """/dev/gpiochipN created by virtrtlab_gpio has correct permissions."""

    def test_gpiochip_mode_is_0660(self, gpio_module):
        """gpiochipN is crw-rw---- (low 9 bits = 0660)."""
        chip = _chip_path()
        mode = os.stat(chip).st_mode & 0o777
        assert mode == 0o660, (
            f"{chip}: expected mode 0660, got {oct(mode)} — "
            "check udev rule: SUBSYSTEM==\"gpio\", SUBSYSTEMS==\"virtrtlab\""
        )

    def test_gpiochip_owner_is_root(self, gpio_module):
        """gpiochipN is owned by root (uid 0)."""
        chip = _chip_path()
        uid = os.stat(chip).st_uid
        assert uid == 0, f"{chip}: expected uid 0 (root), got {uid}"

    def test_gpiochip_group_is_virtrtlab(self, gpio_module):
        """gpiochipN group is 'virtrtlab'."""
        gid = _virtrtlab_gid()
        if gid is None:
            pytest.skip("group 'virtrtlab' does not exist on this system")
        chip = _chip_path()
        chip_gid = os.stat(chip).st_gid
        assert chip_gid == gid, (
            f"{chip}: expected gid {gid} (virtrtlab), got {chip_gid}"
        )

    def test_gpiochip_no_world_write(self, gpio_module):
        """gpiochipN has no world-write bit (o-w)."""
        chip = _chip_path()
        mode = os.stat(chip).st_mode & 0o002
        assert mode == 0, (
            f"{chip}: world-write bit is set — mode={oct(os.stat(chip).st_mode & 0o777)}"
        )

    def test_gpiochip_is_char_device(self, gpio_module):
        """Sanity: the gpiochip path is a character device."""
        chip = _chip_path()
        st = os.stat(chip)
        assert stat.S_ISCHR(st.st_mode), (
            f"{chip}: not a character device (mode={oct(st.st_mode)})"
        )


# ---------------------------------------------------------------------------
# inject sysfs attribute permissions (udev RUN+ rule)
# ---------------------------------------------------------------------------

class TestInjectAttrPermissions:
    """inject sysfs attribute is group-writable after modprobe."""

    def test_inject_attr_exists(self, gpio_module):
        """inject attribute file is present in sysfs."""
        inject = f"{DEVICES_ROOT}/gpio0/inject"
        assert os.path.exists(inject), f"inject attr not found: {inject}"

    def test_inject_attr_group_writable(self, gpio_module):
        """inject has the group-write bit set (g+w)."""
        inject = f"{DEVICES_ROOT}/gpio0/inject"
        if not os.path.exists(inject):
            pytest.skip(f"inject attr not found: {inject}")
        mode = os.stat(inject).st_mode
        assert mode & 0o020, (
            f"inject not group-writable: {oct(mode)} — "
            "check udev rule: ACTION==\"add\", SUBSYSTEM==\"virtrtlab\", "
            "ATTR{{type}}==\"gpio\", RUN+=\"/bin/chmod g+w /sys%p/inject\""
        )

    def test_inject_no_world_write(self, gpio_module):
        """inject has no world-write bit (o-w)."""
        inject = f"{DEVICES_ROOT}/gpio0/inject"
        if not os.path.exists(inject):
            pytest.skip(f"inject attr not found: {inject}")
        mode = os.stat(inject).st_mode & 0o002
        assert mode == 0, (
            f"inject world-write bit is set: {oct(os.stat(inject).st_mode & 0o777)}"
        )
