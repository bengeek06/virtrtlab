# SPDX-License-Identifier: MIT

"""test_up_contract.py — unit tests for the AUT integration contract emitted by cmd_up."""

import json

import pytest

import virtrtlabctl as ctl
from conftest import make_args


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_profile(*device_specs):
    """Return a minimal profile dict from (type, count) pairs."""
    return {
        "devices": [{"type": t, "count": c} for t, c in device_specs],
        "build": {},
        "bus": {},
    }


# ---------------------------------------------------------------------------
# Fixture: fake_gpio adds a gpio0 device to fake_sysfs with chip_path
# and optionally sysfs_base.
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_gpio_with_base(fake_sysfs):
    """gpio0 with chip_path and sysfs_base exposed."""
    dev = fake_sysfs["sysfs"] / "devices" / "gpio0"
    dev.mkdir(parents=True)
    (dev / "type").write_text("gpio\n")
    (dev / "chip_path").write_text("/dev/gpiochip4\n")
    (dev / "sysfs_base").write_text("200\n")
    return fake_sysfs


@pytest.fixture
def fake_gpio_no_base(fake_sysfs):
    """gpio0 with chip_path but without sysfs_base (no legacy ABI)."""
    dev = fake_sysfs["sysfs"] / "devices" / "gpio0"
    dev.mkdir(parents=True)
    (dev / "type").write_text("gpio\n")
    (dev / "chip_path").write_text("/dev/gpiochip4\n")
    # sysfs_base intentionally absent
    return fake_sysfs


# ---------------------------------------------------------------------------
# UART contract
# ---------------------------------------------------------------------------


class TestUartContract:
    def test_single_uart_paths(self, fake_sysfs):
        profile = _make_profile(("uart", 1))
        contract = ctl._resolve_aut_contract(profile)
        assert len(contract) == 1
        entry = contract[0]
        assert entry["name"] == "uart0"
        assert entry["type"] == "uart"
        assert entry["aut_path"] == "/dev/ttyVIRTLAB0"
        assert entry["wire_path"] == "/dev/virtrtlab-wire0"
        assert "uart0.sock" in entry["socket_path"]

    def test_single_uart_env(self, fake_sysfs):
        profile = _make_profile(("uart", 1))
        contract = ctl._resolve_aut_contract(profile)
        env = contract[0]["env"]
        assert env.get("VIRTRTLAB_UART0") == "/dev/ttyVIRTLAB0"

    def test_two_uart_instances_indexed(self, fake_sysfs):
        profile = _make_profile(("uart", 2))
        contract = ctl._resolve_aut_contract(profile)
        assert len(contract) == 2
        assert contract[0]["name"] == "uart0"
        assert contract[1]["name"] == "uart1"
        assert contract[1]["aut_path"] == "/dev/ttyVIRTLAB1"
        assert "VIRTRTLAB_UART1" in contract[1]["env"]

    def test_multiple_uart_entries_cumulative_index(self, fake_sysfs):
        """Two separate [[devices]] uart blocks must use cumulative index."""
        profile = _make_profile(("uart", 1), ("uart", 1))
        contract = ctl._resolve_aut_contract(profile)
        assert contract[0]["name"] == "uart0"
        assert contract[1]["name"] == "uart1"


# ---------------------------------------------------------------------------
# GPIO contract — with sysfs_base
# ---------------------------------------------------------------------------


class TestGpioContractWithBase:
    def test_gpio_chip_path(self, fake_gpio_with_base):
        profile = _make_profile(("gpio", 1))
        contract = ctl._resolve_aut_contract(profile)
        assert len(contract) == 1
        entry = contract[0]
        assert entry["name"] == "gpio0"
        assert entry["type"] == "gpio"
        assert entry["chip_path"] == "/dev/gpiochip4"

    def test_gpio_sysfs_base_included(self, fake_gpio_with_base):
        profile = _make_profile(("gpio", 1))
        contract = ctl._resolve_aut_contract(profile)
        entry = contract[0]
        assert entry.get("sysfs_base") == 200

    def test_gpio_env_vars_complete(self, fake_gpio_with_base):
        profile = _make_profile(("gpio", 1))
        contract = ctl._resolve_aut_contract(profile)
        env = contract[0]["env"]
        assert env.get("VIRTRTLAB_GPIOCHIP0") == "/dev/gpiochip4"
        assert env.get("VIRTRTLAB_GPIOBASE0") == "200"
        assert "VIRTRTLAB_GPIOCTRL0" in env

    def test_gpio_control_path(self, fake_gpio_with_base):
        profile = _make_profile(("gpio", 1))
        contract = ctl._resolve_aut_contract(profile)
        ctrl = contract[0]["control_path"]
        assert "gpio0" in ctrl


# ---------------------------------------------------------------------------
# GPIO contract — without sysfs_base (graceful degradation)
# ---------------------------------------------------------------------------


class TestGpioContractNoBase:
    def test_gpio_no_sysfs_base_key_absent(self, fake_gpio_no_base):
        profile = _make_profile(("gpio", 1))
        contract = ctl._resolve_aut_contract(profile)
        assert "sysfs_base" not in contract[0]

    def test_gpio_gpiobase_env_absent(self, fake_gpio_no_base):
        profile = _make_profile(("gpio", 1))
        contract = ctl._resolve_aut_contract(profile)
        env = contract[0]["env"]
        assert "VIRTRTLAB_GPIOBASE0" not in env

    def test_gpio_other_env_vars_present_without_base(self, fake_gpio_no_base):
        profile = _make_profile(("gpio", 1))
        contract = ctl._resolve_aut_contract(profile)
        env = contract[0]["env"]
        assert "VIRTRTLAB_GPIOCHIP0" in env
        assert "VIRTRTLAB_GPIOCTRL0" in env

    def test_gpio_no_base_warns_to_stderr(self, fake_gpio_no_base, capsys):
        profile = _make_profile(("gpio", 1))
        ctl._resolve_aut_contract(profile)
        err = capsys.readouterr().err
        assert "gpio0" in err
        assert "sysfs_base" in err.lower() or "legacy" in err.lower()


# ---------------------------------------------------------------------------
# Mixed UART + GPIO contract
# ---------------------------------------------------------------------------


class TestMixedContract:
    def test_uart_and_gpio_both_present(self, fake_gpio_with_base):
        profile = _make_profile(("uart", 1), ("gpio", 1))
        contract = ctl._resolve_aut_contract(profile)
        assert len(contract) == 2
        types = [e["type"] for e in contract]
        assert "uart" in types
        assert "gpio" in types

    def test_ordering_matches_profile_declaration(self, fake_gpio_with_base):
        profile = _make_profile(("uart", 1), ("gpio", 1))
        contract = ctl._resolve_aut_contract(profile)
        assert contract[0]["type"] == "uart"
        assert contract[1]["type"] == "gpio"

    def test_two_gpio_instances_indexed(self, fake_sysfs):
        """Two GPIO instances produce gpio0 and gpio1 entries."""
        for idx in range(2):
            dev = fake_sysfs["sysfs"] / "devices" / f"gpio{idx}"
            dev.mkdir(parents=True)
            (dev / "chip_path").write_text(f"/dev/gpiochip{idx}\n")
            (dev / "sysfs_base").write_text(f"{200 + idx * 8}\n")

        profile = _make_profile(("gpio", 2))
        contract = ctl._resolve_aut_contract(profile)
        assert len(contract) == 2
        assert contract[0]["name"] == "gpio0"
        assert contract[1]["name"] == "gpio1"
        assert contract[1]["sysfs_base"] == 208
        assert "VIRTRTLAB_GPIOBASE1" in contract[1]["env"]


# ---------------------------------------------------------------------------
# JSON output from cmd_up (mocked path — no module loading)
# ---------------------------------------------------------------------------


class TestCmdUpContractJsonOutput:
    """Verify the JSON schema of cmd_up contract output using _resolve_aut_contract."""

    def test_json_schema_uart(self, fake_sysfs, capsys):
        profile = _make_profile(("uart", 1))
        contract = ctl._resolve_aut_contract(profile)
        ctl._emit({"devices": contract}, True)
        data = json.loads(capsys.readouterr().out)
        assert "devices" in data
        device = data["devices"][0]
        assert device["type"] == "uart"
        assert device["name"] == "uart0"
        assert "aut_path" in device
        assert "env" in device
        assert "VIRTRTLAB_UART0" in device["env"]

    def test_json_schema_gpio_with_base(self, fake_gpio_with_base, capsys):
        profile = _make_profile(("gpio", 1))
        contract = ctl._resolve_aut_contract(profile)
        ctl._emit({"devices": contract}, True)
        data = json.loads(capsys.readouterr().out)
        device = data["devices"][0]
        assert device["type"] == "gpio"
        assert device["sysfs_base"] == 200
        assert device["env"]["VIRTRTLAB_GPIOBASE0"] == "200"

    def test_json_schema_gpio_no_base(self, fake_gpio_no_base, capsys):
        profile = _make_profile(("gpio", 1))
        contract = ctl._resolve_aut_contract(profile)
        ctl._emit({"devices": contract}, True)
        data = json.loads(capsys.readouterr().out)
        device = data["devices"][0]
        assert "sysfs_base" not in device
        assert "VIRTRTLAB_GPIOBASE0" not in device["env"]
