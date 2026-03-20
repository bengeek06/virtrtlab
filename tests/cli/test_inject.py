# SPDX-License-Identifier: MIT

"""test_inject.py — unit tests for virtrtlabctl inject subcommand."""

import json

import pytest

import virtrtlabctl as ctl
from conftest import make_args


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_gpio(fake_sysfs):
    """Add a gpio0 device subtree with an inject attribute to the fake sysfs."""
    dev = fake_sysfs["sysfs"] / "devices" / "gpio0"
    dev.mkdir(parents=True)
    (dev / "type").write_text("gpio\n")
    (dev / "bus").write_text("vrtlbus0\n")
    (dev / "num_lines").write_text("8\n")
    (dev / "chip_path").write_text("/dev/gpiochip0\n")
    # write_text("") simulates the wo attr existing; tests read it back to verify writes
    (dev / "inject").write_text("")
    return fake_sysfs


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestInjectHappyPath:
    def test_inject_line_0_value_1(self, fake_gpio):
        args = make_args("inject", "gpio0", "0", "1")
        rc = ctl.cmd_inject(args)
        assert rc == 0
        inject = fake_gpio["sysfs"] / "devices" / "gpio0" / "inject"
        assert inject.read_text() == "0:1"

    def test_inject_line_7_value_0(self, fake_gpio):
        args = make_args("inject", "gpio0", "7", "0")
        rc = ctl.cmd_inject(args)
        assert rc == 0
        inject = fake_gpio["sysfs"] / "devices" / "gpio0" / "inject"
        assert inject.read_text() == "7:0"

    def test_inject_boundary_line_values(self, fake_gpio):
        for line in range(8):
            args = make_args("inject", "gpio0", str(line), "1")
            assert ctl.cmd_inject(args) == 0

    def test_inject_json_output(self, fake_gpio, capsys):
        args = make_args("--json", "inject", "gpio0", "3", "1")
        rc = ctl.cmd_inject(args)
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out == {"device": "gpio0", "line": 3, "value": 1, "status": "ok"}

    def test_inject_text_output(self, fake_gpio, capsys):
        args = make_args("inject", "gpio0", "2", "0")
        ctl.cmd_inject(args)
        assert "gpio0" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Invalid line argument (rejected by argparse)
# ---------------------------------------------------------------------------


class TestInjectInvalidLine:
    def test_line_negative(self):
        with pytest.raises(SystemExit) as exc:
            make_args("inject", "gpio0", "-1", "1")
        assert exc.value.code != 0

    def test_line_too_large(self):
        with pytest.raises(SystemExit) as exc:
            make_args("inject", "gpio0", "8", "1")
        assert exc.value.code != 0

    def test_line_not_integer(self):
        with pytest.raises(SystemExit) as exc:
            make_args("inject", "gpio0", "abc", "1")
        assert exc.value.code != 0

    def test_line_float_rejected(self):
        with pytest.raises(SystemExit) as exc:
            make_args("inject", "gpio0", "1.5", "1")
        assert exc.value.code != 0


# ---------------------------------------------------------------------------
# Invalid value argument (rejected by argparse)
# ---------------------------------------------------------------------------


class TestInjectInvalidValue:
    def test_value_2(self):
        with pytest.raises(SystemExit) as exc:
            make_args("inject", "gpio0", "0", "2")
        assert exc.value.code != 0

    def test_value_negative(self):
        with pytest.raises(SystemExit) as exc:
            make_args("inject", "gpio0", "0", "-1")
        assert exc.value.code != 0

    def test_value_not_integer(self):
        with pytest.raises(SystemExit) as exc:
            make_args("inject", "gpio0", "0", "high")
        assert exc.value.code != 0


# ---------------------------------------------------------------------------
# Device not found
# ---------------------------------------------------------------------------


class TestInjectDeviceNotFound:
    def test_device_absent_from_sysfs(self, fake_sysfs):
        args = make_args("inject", "gpio99", "0", "1")
        with pytest.raises(ctl.VirtrtlabError) as exc:
            ctl.cmd_inject(args)
        assert exc.value.exit_code == 4
        assert "gpio99" in str(exc.value)

    def test_device_has_no_inject_attr(self, fake_sysfs):
        """Device directory exists but exposes no inject attribute."""
        dev = fake_sysfs["sysfs"] / "devices" / "gpio0"
        dev.mkdir(parents=True)
        (dev / "type").write_text("gpio\n")
        # inject file deliberately absent
        args = make_args("inject", "gpio0", "0", "1")
        with pytest.raises(ctl.VirtrtlabError) as exc:
            ctl.cmd_inject(args)
        assert exc.value.exit_code == 4
        assert "inject" in str(exc.value).lower()
