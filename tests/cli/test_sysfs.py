"""
test_sysfs.py — unit tests for sysfs CRUD commands.

All tests use the fake_sysfs / fake_uart fixtures from conftest.py
and never touch the real kernel sysfs tree. No root required.
"""

import json

import pytest

from conftest import ctl, make_args


# ---------------------------------------------------------------------------
# cmd_get
# ---------------------------------------------------------------------------

class TestCmdGet:
    """get <device> <attr> and get bus <attr>."""

    def test_get_device_attr(self, fake_uart, capsys):
        args = make_args("get", "uart0", "baud")
        rc = ctl.cmd_get(args)
        assert rc == 0
        assert capsys.readouterr().out.strip() == "115200"

    def test_get_bus_attr(self, fake_sysfs, capsys):
        args = make_args("get", "bus", "state")
        rc = ctl.cmd_get(args)
        assert rc == 0
        assert capsys.readouterr().out.strip() == "up"

    def test_get_strips_trailing_newline(self, fake_uart, capsys):
        args = make_args("get", "uart0", "latency_ns")
        ctl.cmd_get(args)
        out = capsys.readouterr().out
        assert out == "0\n"  # one newline from print(), value has no extra

    def test_get_missing_attr_exit4(self, fake_uart):
        args = make_args("get", "uart0", "nonexistent")
        with pytest.raises(ctl.VirtrtlabError) as exc_info:
            ctl.cmd_get(args)
        assert exc_info.value.exit_code == 4

    def test_get_missing_device_exit4(self, fake_sysfs):
        args = make_args("get", "uart99", "baud")
        with pytest.raises(ctl.VirtrtlabError) as exc_info:
            ctl.cmd_get(args)
        assert exc_info.value.exit_code == 4

    def test_get_json_output(self, fake_uart, capsys):
        args = make_args("--json", "get", "uart0", "baud")
        rc = ctl.cmd_get(args)
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data == {"target": "uart0", "attr": "baud", "value": "115200"}


# ---------------------------------------------------------------------------
# cmd_set
# ---------------------------------------------------------------------------

class TestCmdSet:
    """set <device> <attr>=<value> [...]."""

    def test_set_single_attr(self, fake_uart):
        baud_path = fake_uart["sysfs"] / "devices" / "uart0" / "baud"
        args = make_args("set", "uart0", "baud=9600")
        rc = ctl.cmd_set(args)
        assert rc == 0
        assert baud_path.read_text() == "9600"

    def test_set_multiple_attrs(self, fake_uart):
        sysfs = fake_uart["sysfs"] / "devices" / "uart0"
        args = make_args("set", "uart0", "baud=9600", "latency_ns=500")
        rc = ctl.cmd_set(args)
        assert rc == 0
        assert (sysfs / "baud").read_text() == "9600"
        assert (sysfs / "latency_ns").read_text() == "500"

    def test_set_bus_attr(self, fake_sysfs):
        state_path = fake_sysfs["sysfs"] / "buses" / "vrtlbus0" / "state"
        args = make_args("set", "bus", "state=down")
        rc = ctl.cmd_set(args)
        assert rc == 0
        assert state_path.read_text() == "down"

    def test_set_malformed_no_equals_exit2(self, fake_uart):
        args = make_args("set", "uart0", "baud")
        with pytest.raises(ctl.VirtrtlabError) as exc_info:
            ctl.cmd_set(args)
        assert exc_info.value.exit_code == 2

    def test_set_malformed_empty_attr_exit2(self, fake_uart):
        args = make_args("set", "uart0", "=9600")
        with pytest.raises(ctl.VirtrtlabError) as exc_info:
            ctl.cmd_set(args)
        assert exc_info.value.exit_code == 2

    def test_set_missing_attr_exit4(self, fake_uart):
        args = make_args("set", "uart0", "nonexistent=1")
        with pytest.raises(ctl.VirtrtlabError) as exc_info:
            ctl.cmd_set(args)
        assert exc_info.value.exit_code == 4

    def test_set_value_with_equals_sign(self, fake_uart):
        """Value may contain '=' — only the first '=' is the separator."""
        baud_path = fake_uart["sysfs"] / "devices" / "uart0" / "baud"
        args = make_args("set", "uart0", "baud=1=2")
        ctl.cmd_set(args)
        assert baud_path.read_text() == "1=2"


# ---------------------------------------------------------------------------
# cmd_stats
# ---------------------------------------------------------------------------

class TestCmdStats:
    """stats <device>."""

    def test_stats_human(self, fake_uart, capsys):
        args = make_args("stats", "uart0")
        rc = ctl.cmd_stats(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "tx_bytes" in out
        assert "rx_bytes" in out
        assert "1048576" in out

    def test_stats_json(self, fake_uart, capsys):
        args = make_args("--json", "stats", "uart0")
        rc = ctl.cmd_stats(args)
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["device"] == "uart0"
        assert "tx_bytes" in data["stats"]
        assert data["stats"]["tx_bytes"] == 1048576  # int, not str

    def test_stats_missing_device_exit4(self, fake_sysfs):
        args = make_args("stats", "uart99")
        with pytest.raises(ctl.VirtrtlabError) as exc_info:
            ctl.cmd_stats(args)
        assert exc_info.value.exit_code == 4


# ---------------------------------------------------------------------------
# cmd_reset
# ---------------------------------------------------------------------------

class TestCmdReset:
    """reset <device>."""

    def test_reset_writes_zero(self, fake_uart):
        reset_path = fake_uart["sysfs"] / "devices" / "uart0" / "stats" / "reset"
        reset_path.write_text("42\n")  # set non-zero first
        args = make_args("reset", "uart0")
        rc = ctl.cmd_reset(args)
        assert rc == 0
        assert reset_path.read_text() == "0"

    def test_reset_missing_device_exit4(self, fake_sysfs):
        args = make_args("reset", "uart99")
        with pytest.raises(ctl.VirtrtlabError) as exc_info:
            ctl.cmd_reset(args)
        assert exc_info.value.exit_code == 4


# ---------------------------------------------------------------------------
# cmd_list
# ---------------------------------------------------------------------------

class TestCmdList:
    """list buses / list devices [--type]."""

    def test_list_buses(self, fake_sysfs, capsys):
        args = make_args("list", "buses")
        rc = ctl.cmd_list(args)
        assert rc == 0
        assert "vrtlbus0" in capsys.readouterr().out

    def test_list_buses_json(self, fake_sysfs, capsys):
        args = make_args("--json", "list", "buses")
        rc = ctl.cmd_list(args)
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert "vrtlbus0" in data["buses"]

    def test_list_devices(self, fake_uart, capsys):
        args = make_args("list", "devices")
        rc = ctl.cmd_list(args)
        assert rc == 0
        assert "uart0" in capsys.readouterr().out

    def test_list_devices_type_filter_match(self, fake_uart, capsys):
        args = make_args("list", "devices", "--type", "uart")
        rc = ctl.cmd_list(args)
        assert rc == 0
        assert "uart0" in capsys.readouterr().out

    def test_list_devices_type_filter_no_match(self, fake_uart, capsys):
        args = make_args("list", "devices", "--type", "gpio")
        rc = ctl.cmd_list(args)
        assert rc == 0
        assert capsys.readouterr().out.strip() == ""

    def test_list_devices_json(self, fake_uart, capsys):
        args = make_args("--json", "list", "devices")
        rc = ctl.cmd_list(args)
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        names = [d["name"] for d in data["devices"]]
        assert "uart0" in names

    def test_list_buses_missing_sysfs_exit4(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ctl, "SYSFS_ROOT", str(tmp_path / "nonexistent"))
        args = make_args("list", "buses")
        with pytest.raises(ctl.VirtrtlabError) as exc_info:
            ctl.cmd_list(args)
        assert exc_info.value.exit_code == 4

    def test_list_enabled_field(self, fake_uart, capsys):
        args = make_args("--json", "list", "devices")
        ctl.cmd_list(args)
        data = json.loads(capsys.readouterr().out)
        uart0 = next(d for d in data["devices"] if d["name"] == "uart0")
        assert uart0["enabled"] is True
