# SPDX-License-Identifier: MIT

"""
test_profile.py — unit tests for lab profile resolution.

All tests run without kernel modules or root — they exercise
_resolve_profile(), _find_ko(), and MODULE_MAP in pure Python.
"""

import os
import tempfile

import pytest

from conftest import ctl, make_args


# ---------------------------------------------------------------------------
# _resolve_profile — inline flags
# ---------------------------------------------------------------------------

class TestResolveProfileInline:
    """Profile built from --uart / --gpio inline flags."""

    def test_uart_only(self):
        args = make_args("up", "--uart", "2")
        profile = ctl._resolve_profile(args)
        assert len(profile["devices"]) == 1
        assert profile["devices"][0] == {"type": "uart", "count": 2}

    def test_gpio_only(self):
        args = make_args("up", "--gpio", "1")
        profile = ctl._resolve_profile(args)
        assert profile["devices"][0] == {"type": "gpio", "count": 1}

    def test_uart_and_gpio(self):
        args = make_args("up", "--uart", "2", "--gpio", "1")
        profile = ctl._resolve_profile(args)
        types = [d["type"] for d in profile["devices"]]
        assert "uart" in types and "gpio" in types

    def test_build_and_bus_empty_for_inline(self):
        args = make_args("up", "--uart", "1")
        profile = ctl._resolve_profile(args)
        assert profile["build"] == {}
        assert profile["bus"] == {}


# ---------------------------------------------------------------------------
# _resolve_profile — TOML file
# ---------------------------------------------------------------------------

class TestResolveProfileToml:
    """Profile loaded from a TOML file."""

    def _write_toml(self, content: bytes) -> str:
        f = tempfile.NamedTemporaryFile(suffix=".toml", delete=False)
        f.write(content)
        f.close()
        return f.name

    def test_basic_toml(self):
        path = self._write_toml(
            b"[[devices]]\ntype = \"uart\"\ncount = 3\n"
        )
        try:
            args = make_args("up", "--config", path)
            profile = ctl._resolve_profile(args)
            assert profile["devices"] == [{"type": "uart", "count": 3}]
        finally:
            os.unlink(path)

    def test_toml_with_build_section(self):
        path = self._write_toml(
            b"[build]\nmodule_dir = \"/tmp/mods\"\n[[devices]]\ntype = \"uart\"\ncount = 1\n"
        )
        try:
            args = make_args("up", "--config", path)
            profile = ctl._resolve_profile(args)
            assert profile["build"]["module_dir"] == "/tmp/mods"
        finally:
            os.unlink(path)

    def test_toml_with_bus_seed(self):
        path = self._write_toml(
            b"[bus]\nseed = 42\n[[devices]]\ntype = \"uart\"\ncount = 1\n"
        )
        try:
            args = make_args("up", "--config", path)
            profile = ctl._resolve_profile(args)
            assert profile["bus"]["seed"] == 42
        finally:
            os.unlink(path)

    def test_inline_overrides_toml(self):
        """--uart inline flag overrides the TOML count."""
        path = self._write_toml(
            b"[[devices]]\ntype = \"uart\"\ncount = 1\n"
        )
        try:
            args = make_args("up", "--config", path, "--uart", "4")
            profile = ctl._resolve_profile(args)
            uart = next(d for d in profile["devices"] if d["type"] == "uart")
            assert uart["count"] == 4
        finally:
            os.unlink(path)

    def test_inline_adds_device_not_in_toml(self):
        """--gpio inline adds a gpio device even if absent from TOML."""
        path = self._write_toml(
            b"[[devices]]\ntype = \"uart\"\ncount = 1\n"
        )
        try:
            args = make_args("up", "--config", path, "--gpio", "2")
            profile = ctl._resolve_profile(args)
            types = [d["type"] for d in profile["devices"]]
            assert "gpio" in types
        finally:
            os.unlink(path)

    def test_config_file_not_found_exit2(self):
        args = make_args("up", "--config", "/nonexistent/lab.toml")
        with pytest.raises(ctl.VirtrtlabError) as exc_info:
            ctl._resolve_profile(args)
        assert exc_info.value.exit_code == 2

    def test_invalid_toml_exit2(self):
        path = self._write_toml(b"[[[ invalid toml\n")
        try:
            args = make_args("up", "--config", path)
            with pytest.raises(ctl.VirtrtlabError) as exc_info:
                ctl._resolve_profile(args)
            assert exc_info.value.exit_code == 2
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# _resolve_profile — validation
# ---------------------------------------------------------------------------

class TestResolveProfileValidation:
    """Profile validation: unknown types, missing profile."""

    def test_unknown_type_exit2(self, tmp_path):
        path = tmp_path / "lab.toml"
        path.write_bytes(b"[[devices]]\ntype = \"spi\"\ncount = 1\n")
        args = make_args("up", "--config", str(path))
        with pytest.raises(ctl.VirtrtlabError) as exc_info:
            ctl._resolve_profile(args)
        assert exc_info.value.exit_code == 2
        assert "spi" in str(exc_info.value)

    def test_no_profile_no_inline_exit2(self, tmp_path, monkeypatch):
        """No file found and no inline flags → exit 2."""
        # Run from a directory with no lab.toml
        monkeypatch.chdir(tmp_path)
        args = make_args("up")
        with pytest.raises(ctl.VirtrtlabError) as exc_info:
            ctl._resolve_profile(args)
        assert exc_info.value.exit_code == 2


# ---------------------------------------------------------------------------
# _find_ko
# ---------------------------------------------------------------------------

class TestFindKo:
    """_find_ko search order and failure handling."""

    def test_finds_ko_in_module_dir(self, tmp_path):
        ko = tmp_path / "virtrtlab_uart.ko"
        ko.write_bytes(b"")
        result = ctl._find_ko("virtrtlab_uart", str(tmp_path))
        assert result == ko

    def test_finds_ko_in_cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        ko = tmp_path / "virtrtlab_core.ko"
        ko.write_bytes(b"")
        result = ctl._find_ko("virtrtlab_core")
        assert result.name == "virtrtlab_core.ko"

    def test_not_found_exit1(self, tmp_path, monkeypatch):
        """_find_ko raises VirtrtlabError(exit_code=1) when .ko is nowhere.

        modinfo and uname are stubbed so that installed system modules
        (if any) cannot be discovered by the fallback paths.
        """
        monkeypatch.chdir(tmp_path)
        # Prevent modinfo from finding an installed module on the test machine.
        import subprocess as _sp
        original_run = _sp.run
        def _fake_run(cmd, **kwargs):
            if cmd[0] == "modinfo":
                raise _sp.SubprocessError("stubbed")
            return original_run(cmd, **kwargs)
        monkeypatch.setattr(_sp, "run", _fake_run)
        # Also prevent check_output(uname -r) from returning a real path.
        monkeypatch.setattr(_sp, "check_output",
                            lambda *a, **kw: (_ for _ in ()).throw(_sp.SubprocessError("stubbed")))
        with pytest.raises(ctl.VirtrtlabError) as exc_info:
            ctl._find_ko("virtrtlab_uart", str(tmp_path))
        assert exc_info.value.exit_code == 1

    def test_module_dir_takes_priority(self, tmp_path):
        """module_dir is checked before cwd."""
        priority_dir = tmp_path / "priority"
        priority_dir.mkdir()
        other_dir = tmp_path / "other"
        other_dir.mkdir()
        (priority_dir / "virtrtlab_uart.ko").write_bytes(b"priority")
        (other_dir / "virtrtlab_uart.ko").write_bytes(b"other")
        result = ctl._find_ko("virtrtlab_uart", str(priority_dir))
        assert result.parent == priority_dir
