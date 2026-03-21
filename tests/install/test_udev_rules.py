# SPDX-License-Identifier: MIT

"""
test_udev_rules.py — static validation of install/90-virtrtlab.rules (issue #48).

These tests require no kernel modules, no root, and no running daemon.
They validate the udev rules file at the source level so regressions are
caught in CI before any hardware is involved.

Coverage:
  - File can be parsed by udevadm verify (syntax check)
  - gpiochip rule is present with correct match keys and permissions
  - inject RUN+ rule is present with correct match keys
  - wire device rule is present
  - TTY rule is present
  - No world-write mode appears in any MODE= directive
"""

import os
import subprocess
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Locate the rules file relative to this test file
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]
RULES_FILE = _REPO_ROOT / "install" / "90-virtrtlab.rules"


@pytest.fixture(scope="module")
def rules_text():
    """Return the contents of 90-virtrtlab.rules as a string."""
    if not RULES_FILE.exists():
        pytest.skip(f"Rules file not found: {RULES_FILE}")
    return RULES_FILE.read_text()


# ---------------------------------------------------------------------------
# Syntax check via udevadm
# ---------------------------------------------------------------------------

class TestUdevRulesSyntax:
    """udevadm can parse the rules file without errors."""

    def test_udevadm_verify_passes(self):
        """udevadm verify reports no errors on 90-virtrtlab.rules."""
        if not RULES_FILE.exists():
            pytest.skip(f"Rules file not found: {RULES_FILE}")
        result = subprocess.run(
            ["udevadm", "verify", str(RULES_FILE)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"udevadm verify failed:\n{result.stderr}"
        )


# ---------------------------------------------------------------------------
# /dev/gpiochipN rule
# ---------------------------------------------------------------------------

class TestGpiochipRule:
    """udev rule for /dev/gpiochipN sets virtrtlab group and 0660 mode."""

    def test_gpiochip_subsystem_match(self, rules_text):
        """Rule matches SUBSYSTEM==\"gpio\"."""
        assert 'SUBSYSTEM=="gpio"' in rules_text, (
            "Missing SUBSYSTEM==\"gpio\" in rules file"
        )

    def test_gpiochip_kernel_match(self, rules_text):
        """Rule matches KERNEL==\"gpiochip*\"."""
        assert 'KERNEL=="gpiochip*"' in rules_text, (
            "Missing KERNEL==\"gpiochip*\" in rules file"
        )

    def test_gpiochip_parent_subsystem_match(self, rules_text):
        """Rule uses SUBSYSTEMS==\"virtrtlab\" to avoid matching unrelated chips."""
        assert 'SUBSYSTEMS=="virtrtlab"' in rules_text, (
            "Missing SUBSYSTEMS==\"virtrtlab\" guard — rule would match all gpiochips"
        )

    def test_gpiochip_group_virtrtlab(self, rules_text):
        """Rule sets GROUP=\"virtrtlab\"."""
        assert 'GROUP="virtrtlab"' in rules_text, (
            "Missing GROUP=\"virtrtlab\" in gpiochip rule"
        )

    def test_gpiochip_mode_0660(self, rules_text):
        """Rule sets MODE=\"0660\" for gpiochip devices."""
        assert 'MODE="0660"' in rules_text, (
            "Missing MODE=\"0660\" in rules file"
        )


# ---------------------------------------------------------------------------
# inject sysfs attribute rule
# ---------------------------------------------------------------------------

class TestInjectRule:
    """udev rule fires chmod g+w on /sys/.../inject at device registration."""

    def test_inject_action_add(self, rules_text):
        """Rule fires on ACTION==\"add\"."""
        assert 'ACTION=="add"' in rules_text, (
            "Missing ACTION==\"add\" in inject rule"
        )

    def test_inject_subsystem_virtrtlab(self, rules_text):
        """Rule targets SUBSYSTEM==\"virtrtlab\"."""
        assert 'SUBSYSTEM=="virtrtlab"' in rules_text, (
            "Missing SUBSYSTEM==\"virtrtlab\" in inject rule"
        )

    def test_inject_attr_type_gpio(self, rules_text):
        """Rule scopes to GPIO devices via ATTR{type}==\"gpio\"."""
        assert 'ATTR{type}=="gpio"' in rules_text, (
            "Missing ATTR{type}==\"gpio\" guard — rule would match all virtrtlab devices"
        )

    def test_inject_run_chmod_g_plus_w(self, rules_text):
        """Rule runs chmod g+w on the sysfs inject attribute."""
        assert 'RUN+="/bin/chmod g+w' in rules_text, (
            "Missing RUN+=\"/bin/chmod g+w ...\" in inject rule"
        )

    def test_inject_run_uses_sysfs_path(self, rules_text):
        """Rule uses /sys%p to resolve the correct sysfs path."""
        assert "/sys%p/inject" in rules_text, (
            "inject chmod RUN+ command should use /sys%p/inject (not a hardcoded path)"
        )


# ---------------------------------------------------------------------------
# Other device rules
# ---------------------------------------------------------------------------

class TestWireAndTtyRules:
    """Wire char device and TTY node rules are present."""

    def test_wire_device_rule(self, rules_text):
        """Rule for virtrtlab* wire devices is present."""
        assert 'KERNEL=="virtrtlab*"' in rules_text

    def test_tty_rule(self, rules_text):
        """Rule for ttyVIRTLAB* UART nodes is present."""
        assert 'KERNEL=="ttyVIRTLAB*"' in rules_text


# ---------------------------------------------------------------------------
# Safety: no world-write modes
# ---------------------------------------------------------------------------

class TestNoWorldWrite:
    """No MODE= in the rules file grants world-write access."""

    def test_no_world_write_mode(self, rules_text):
        """None of the MODE= values have the world-write bit (LSB of mode octet)."""
        import re
        modes = re.findall(r'MODE="(\d+)"', rules_text)
        for mode_str in modes:
            # mode_str is an octal string like "0660"
            mode_int = int(mode_str, 8)
            assert not (mode_int & 0o002), (
                f"MODE=\"{mode_str}\" has world-write bit set"
            )
