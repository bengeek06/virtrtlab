#!/usr/bin/env python3
# SPDX-License-Identifier: MIT

"""virtrtlabctl — VirtRTLab control CLI."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from typing import Any, cast

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SYSFS_ROOT = "/sys/kernel/virtrtlab"
RUN_DIR = "/run/virtrtlab"
# Resolved at import time:
#   1. $VIRTRTLABD env var (explicit override)
#   2. repo-relative daemon/virtrtlabd (development layout)
#   3. virtrtlabd on PATH (installed layout)
_SCRIPT_DIR = Path(__file__).resolve().parent
_SCRIPT_PATH = _SCRIPT_DIR / Path(__file__).name
_DAEMON_RELATIVE = _SCRIPT_DIR.parent / "daemon" / "virtrtlabd"
DAEMON_BIN: str = os.environ.get(
    "VIRTRTLABD",
    str(_DAEMON_RELATIVE)
    if _DAEMON_RELATIVE.exists()
    else (shutil.which("virtrtlabd") or "virtrtlabd"),
)
KNOWN_MODULES = ["virtrtlab_core", "virtrtlab_uart", "virtrtlab_gpio"]

# type → (ko filename, insmod parameter name)
MODULE_MAP: dict[str, tuple[str, str]] = {
    "uart": ("virtrtlab_uart.ko", "num_uart_devices"),
    "gpio": ("virtrtlab_gpio.ko", "num_gpio_devs"),
}

# ---------------------------------------------------------------------------
# Error class
# ---------------------------------------------------------------------------


class VirtrtlabError(Exception):
    def __init__(self, message: str, exit_code: int = 1):
        super().__init__(message)
        self.exit_code = exit_code


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sudo_prefix(no_sudo: bool) -> list[str]:
    # Skip sudo when already running as root (e.g. under sudo pytest):
    # Popen(["sudo", daemon_bin]) stores the sudo PID, not the daemon's PID,
    # which breaks pid-file tracking. Running as root needs no wrapper.
    return [] if (no_sudo or os.geteuid() == 0) else ["sudo"]


def _run_cmd(
    cmd: list[str],
    *,
    check: bool = True,
    capture: bool = False,
    exit_code: int = 1,
) -> subprocess.CompletedProcess[str]:
    kwargs: dict[str, Any] = {}
    if capture:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    try:
        return subprocess.run(cmd, check=check, text=True, **kwargs)
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() if exc.stderr else ""
        raise VirtrtlabError(
            f"Command failed: {' '.join(cmd)}" + (f": {stderr}" if stderr else ""),
            exit_code=exit_code,
        ) from exc
    except OSError as exc:
        raise VirtrtlabError(
            f"Command failed: {' '.join(cmd)}: {exc.strerror or exc}",
            exit_code=exit_code,
        ) from exc


def _emit(data: "dict[str, Any] | list[Any] | str", json_flag: bool) -> None:
    if json_flag:
        print(json.dumps(data if not isinstance(data, str) else {"message": data}))
    else:
        if isinstance(data, str):
            print(data)
        elif isinstance(data, list):
            for item in data:
                print(item)
        else:
            for k, v in data.items():
                print(f"{k}: {v}")


# ---------------------------------------------------------------------------
# Lab profile
# ---------------------------------------------------------------------------


def _find_ko(module_name: str, module_dir: str | None = None) -> Path:
    """Return path to <module_name>.ko; raise VirtrtlabError(1) if not found.

    Search order:
      1. module_dir (if given)
      2. ./
      3. modinfo -n <module_name>  (handles make modules_install subdirs;
         tries modinfo, /sbin/modinfo, /usr/sbin/modinfo)
      4. find /lib/modules/<uname -r>/ -name <filename>  (recursive scan)
    """
    filename = f"{module_name}.ko"
    candidates: list[Path] = []
    if module_dir:
        candidates.append(Path(module_dir) / filename)
    candidates.append(Path(".") / filename)
    for path in candidates:
        if path.exists():
            return path

    # Try modinfo (may live in /sbin on some distros, not in user PATH)
    for modinfo_bin in ("modinfo", "/sbin/modinfo", "/usr/sbin/modinfo"):
        try:
            result = subprocess.run(
                [modinfo_bin, "-n", module_name],
                capture_output=True,
                text=True,
                check=True,
            )
            p = Path(result.stdout.strip())
            if p.exists():
                return p
            break  # modinfo ran but returned a non-existent path; don't retry
        except (subprocess.SubprocessError, OSError, FileNotFoundError):
            continue

    # Recursive scan of /lib/modules/<uname -r>/ (handles extra/ subdirs)
    try:
        uname_r = subprocess.check_output(["uname", "-r"], text=True).strip()
        mod_root = Path(f"/lib/modules/{uname_r}")
        for p in mod_root.rglob(filename):
            return p
        candidates.append(mod_root / filename)  # for the error message
    except (subprocess.SubprocessError, OSError):
        pass

    searched = ", ".join(str(p) for p in candidates)
    raise VirtrtlabError(f"{filename} not found (searched: {searched})", exit_code=1)


def _resolve_profile(args: argparse.Namespace) -> dict[str, Any]:
    """Resolve a lab profile from TOML file + inline overrides.

    Returns: {'devices': [{'type': str, 'count': int}, ...],
               'bus':     {...},
               'build':   {'module_dir': str | None, ...}}
    Raises VirtrtlabError(2) on parse/validation errors.
    """
    profile: dict[str, Any] = {"devices": [], "bus": {}, "build": {}}

    # Locate TOML file
    toml_path: Path | None = None
    if getattr(args, "config", None):
        toml_path = Path(args.config)
        if not toml_path.exists():
            raise VirtrtlabError(f"config file not found: {toml_path}", exit_code=2)
    else:
        for candidate in (Path("lab.toml"), Path("/etc/virtrtlab/lab.toml")):
            if candidate.exists():
                toml_path = candidate
                break

    if toml_path is not None:
        with open(toml_path, "rb") as fh:
            try:
                data = tomllib.load(fh)
            except tomllib.TOMLDecodeError as exc:
                raise VirtrtlabError(
                    f"profile parse error: {exc}", exit_code=2
                ) from exc
        try:
            profile["build"] = data.get("build", {})
            profile["bus"] = data.get("bus", {})
            raw_devices = data.get("devices", [])
            if not isinstance(raw_devices, list):
                raise VirtrtlabError(
                    "profile parse error: 'devices' must be an array of tables",
                    exit_code=2,
                )
            profile["devices"] = []
            for i, d in enumerate(raw_devices):
                if not isinstance(d, dict) or "type" not in d:
                    raise VirtrtlabError(
                        f"profile parse error: devices[{i}] missing 'type' key",
                        exit_code=2,
                    )
                try:
                    count = int(d.get("count", 1))
                except (ValueError, TypeError) as exc:
                    raise VirtrtlabError(
                        f"profile parse error: devices[{i}].count must be an integer",
                        exit_code=2,
                    ) from exc
                profile["devices"].append({"type": str(d["type"]), "count": count})
        except VirtrtlabError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise VirtrtlabError(f"profile parse error: {exc}", exit_code=2) from exc

    # Apply inline overrides (--uart N, --gpio N); they override matching entries
    inline: dict[str, int] = {}
    if getattr(args, "uart", None) is not None:
        inline["uart"] = args.uart
    if getattr(args, "gpio", None) is not None:
        inline["gpio"] = args.gpio

    if inline:
        by_type = {d["type"]: i for i, d in enumerate(profile["devices"])}
        for dev_type, count in inline.items():
            if dev_type in by_type:
                profile["devices"][by_type[dev_type]]["count"] = count
            else:
                profile["devices"].append({"type": dev_type, "count": count})

    if not profile["devices"]:
        raise VirtrtlabError(
            "no lab profile found (searched: ./lab.toml, /etc/virtrtlab/lab.toml);"
            " pass --config <file> or use --uart/--gpio inline flags",
            exit_code=2,
        )

    # Validate device types
    for dev in profile["devices"]:
        if dev["type"] not in MODULE_MAP:
            raise VirtrtlabError(f"unknown device type: {dev['type']}", exit_code=2)

    return profile


# ---------------------------------------------------------------------------
# Runtime helpers (T3 / T4)
# ---------------------------------------------------------------------------


def _is_module_loaded(name: str) -> bool:
    """Return True iff the module appears in /proc/modules."""
    try:
        with open("/proc/modules") as fh:
            for line in fh:
                if line.split()[0] == name:
                    return True
    except OSError:
        pass
    return False


def _find_virtrtlabd_pid() -> int | None:
    """Scan /proc for a running virtrtlabd process and return its PID.

    Used to find the actual daemon PID after launch via sudo, where
    Popen.pid is the sudo wrapper PID rather than virtrtlabd's own PID.

    When multiple virtrtlabd instances are running, prefer the one whose
    command line matches the current RUN_DIR. If no such match exists and
    exactly one virtrtlabd process is found, return that PID. If multiple
    candidates exist with no RUN_DIR match, return None to avoid picking
    an arbitrary instance.
    """
    # Prefer a process whose cmdline contains --run-dir <RUN_DIR> or
    # --run-dir=RUN_DIR, fall back to a unique virtrtlabd instance.
    fallback_pid: int | None = None
    virtrtlabd_count = 0
    try:
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                if (entry / "comm").read_text().strip() != "virtrtlabd":
                    continue
                pid = int(entry.name)
                virtrtlabd_count += 1
                cmdline_path = entry / "cmdline"
                try:
                    raw = cmdline_path.read_text()
                except OSError:
                    # If cmdline is unreadable, we cannot match RUN_DIR;
                    # remember this PID only as a potential fallback.
                    if fallback_pid is None:
                        fallback_pid = pid
                    continue
                args = [arg for arg in raw.split("\0") if arg]
                # Match either "--run-dir=<RUN_DIR>" or "--run-dir", RUN_DIR.
                run_dir_match = False
                run_dir_eq = f"--run-dir={RUN_DIR}"
                if run_dir_eq in args:
                    run_dir_match = True
                else:
                    for i, arg in enumerate(args):
                        if arg == "--run-dir" and i + 1 < len(args):
                            if args[i + 1] == RUN_DIR:
                                run_dir_match = True
                                break
                if run_dir_match:
                    return pid
                if fallback_pid is None:
                    fallback_pid = pid
            except OSError:
                continue
    except OSError:
        pass
    # If we saw exactly one virtrtlabd process, return it even if we
    # could not match RUN_DIR, preserving the original behaviour.
    if virtrtlabd_count == 1:
        return fallback_pid
    return None


def _daemon_pid(run_dir: str = RUN_DIR) -> int | None:
    """Return daemon PID if virtrtlabd is alive, else None.

    Validates /proc/<pid>/comm to avoid signalling a PID-recycled process.
    Removes a stale pid-file if the recorded process is gone or mismatched.
    """
    pid_file = Path(run_dir) / "daemon.pid"
    try:
        pid = int(pid_file.read_text().strip())
    except (OSError, ValueError):
        return None
    proc_dir = Path(f"/proc/{pid}")
    if not proc_dir.exists():
        try:
            pid_file.unlink(missing_ok=True)
        except PermissionError:
            pass  # root-owned pid file; stale but cannot remove
        return None
    # Validate the process is actually virtrtlabd (guard against PID reuse)
    comm_path = proc_dir / "comm"
    try:
        comm = comm_path.read_text().strip()
        if comm != "virtrtlabd":
            try:
                pid_file.unlink(missing_ok=True)
            except PermissionError:
                pass  # root-owned pid file; stale but cannot remove
            return None
    except OSError:
        pass  # /proc/<pid>/comm unreadable → assume still ours
    return pid


def _ensure_run_dir(no_sudo: bool) -> None:
    _run_cmd(_sudo_prefix(no_sudo) + ["mkdir", "-p", RUN_DIR], exit_code=1)


def _write_run_file(
    filename: str, content: str, no_sudo: bool, run_dir: str = RUN_DIR
) -> None:
    """Write content to <run_dir>/<filename> via sudo tee (or directly if --no-sudo)."""
    path = Path(run_dir) / filename
    if no_sudo:
        path.write_text(content)
    else:
        try:
            subprocess.run(
                ["sudo", "tee", str(path)],
                input=content,
                text=True,
                stdout=subprocess.DEVNULL,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            raise VirtrtlabError(
                f"failed to write {path}: sudo tee exited {exc.returncode}",
                exit_code=1,
            ) from exc


def _spawn_daemon_detached(
    num_uarts: int, run_dir: str, daemon_bin: str = DAEMON_BIN
) -> subprocess.Popen[Any]:
    """Start virtrtlabd detached from the caller's interactive terminal."""
    return subprocess.Popen(
        [daemon_bin, "--num-uarts", str(num_uarts), "--run-dir", run_dir],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )


def _launch_daemon(no_sudo: bool, num_uarts: int, run_dir: str) -> int | None:
    """Launch virtrtlabd while preserving interactive sudo when needed."""
    pid_file = Path(run_dir) / "daemon.pid"
    if _sudo_prefix(no_sudo):
        _run_cmd(
            [
                "sudo",
                sys.executable,
                str(_SCRIPT_PATH),
                "__spawn-daemon-detached",
                "--num-uarts",
                str(num_uarts),
                "--run-dir",
                run_dir,
                "--pid-file",
                str(pid_file),
                "--daemon-bin",
                DAEMON_BIN,
            ],
            capture=True,
            exit_code=1,
        )
        return None

    proc = _spawn_daemon_detached(num_uarts, run_dir)
    _write_run_file("daemon.pid", str(proc.pid) + "\n", no_sudo, run_dir=run_dir)
    return proc.pid


def _insmod(ko_path: Path, param_name: str, count: int, no_sudo: bool) -> None:
    cmd = _sudo_prefix(no_sudo) + ["insmod", str(ko_path)]
    if param_name:
        cmd.append(f"{param_name}={count}")
    _run_cmd(cmd, exit_code=1)


def _rmmod(module_name: str, no_sudo: bool, *, ignore_error: bool = False) -> None:
    try:
        _run_cmd(_sudo_prefix(no_sudo) + ["rmmod", module_name], exit_code=1)
    except VirtrtlabError:
        if not ignore_error:
            raise


def _stop_daemon(no_sudo: bool, run_dir: str = RUN_DIR) -> None:
    """SIGTERM the daemon, wait 5 s, SIGKILL if still alive."""
    pid = _daemon_pid(run_dir)
    if pid is None:
        return
    prefix = _sudo_prefix(no_sudo)
    subprocess.run(prefix + ["kill", "-TERM", str(pid)], check=False)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if not Path(f"/proc/{pid}").exists():
            break
        time.sleep(0.1)
    if Path(f"/proc/{pid}").exists():
        subprocess.run(prefix + ["kill", "-KILL", str(pid)], check=False)
    pid_file = Path(run_dir) / "daemon.pid"
    if pid_file.exists():
        subprocess.run(prefix + ["rm", "-f", str(pid_file)], check=False)


def _poll_sockets(sock_paths: list[Path], timeout: float = 5.0) -> None:
    """Block until all sock_paths exist; raise VirtrtlabError(3) on timeout."""
    deadline = time.monotonic() + timeout
    remaining = list(sock_paths)
    while remaining:
        remaining = [p for p in remaining if not p.exists()]
        if not remaining:
            break
        if time.monotonic() >= deadline:
            missing = ", ".join(str(p) for p in remaining)
            raise VirtrtlabError(f"timeout waiting for sockets: {missing}", exit_code=3)
        time.sleep(0.1)


def _expected_sockets(profile: dict[str, Any]) -> list[Path]:
    """Return expected socket paths for UART devices in the profile."""
    run = Path(RUN_DIR)
    return [
        run / f"uart{i}.sock"
        for dev in profile["devices"]
        if dev["type"] == "uart"
        for i in range(dev["count"])
    ]


def _modules_load_order(profile: dict[str, Any]) -> list[str]:
    """Return module names in load order: core first, then device modules."""
    names: list[str] = ["virtrtlab_core"]
    for dev in profile["devices"]:
        name = MODULE_MAP[dev["type"]][0].removesuffix(".ko")
        if name not in names:
            names.append(name)
    return names


def _resolve_aut_contract(
    profile: dict[str, Any], run_dir: str = RUN_DIR
) -> list[dict[str, Any]]:
    """Resolve the AUT integration contract for every device in the profile.

    For UART devices the paths are derived deterministically from the instance
    index (no sysfs read needed for the TTY path).

    For GPIO devices, chip_path is read from sysfs; VirtrtlabError(exit_code=4)
    is raised if it is missing or empty (the module is not loaded or the contract
    would violate the device-contract "empty = fail fast" rule).  If sysfs_base
    is absent (host kernel built without CONFIG_GPIO_SYSFS), the key and its
    VIRTRTLAB_GPIOBASE<N> env var are omitted; a warning is emitted on stderr
    and stored in entry["warnings"] for JSON consumers.

    Returns a list of device contract dicts in profile declaration order.
    """
    contract: list[dict[str, Any]] = []
    uart_idx = 0
    gpio_idx = 0

    for dev in profile["devices"]:
        count: int = dev["count"]
        dev_type: str = dev["type"]

        if dev_type == "uart":
            for i in range(count):
                idx = uart_idx + i
                aut_path = f"/dev/ttyVIRTLAB{idx}"
                contract.append(
                    {
                        "name": f"uart{idx}",
                        "type": "uart",
                        "aut_path": aut_path,
                        "wire_path": f"/dev/virtrtlab-wire{idx}",
                        "socket_path": f"{run_dir}/uart{idx}.sock",
                        "env": {
                            f"VIRTRTLAB_UART{idx}": aut_path,
                        },
                    }
                )
            uart_idx += count

        elif dev_type == "gpio":
            for i in range(count):
                idx = gpio_idx + i
                ctrl = f"{SYSFS_ROOT}/devices/gpio{idx}"
                chip_path_file = Path(ctrl) / "chip_path"
                sysfs_base_file = Path(ctrl) / "sysfs_base"

                if not chip_path_file.exists():
                    raise VirtrtlabError(
                        f"gpio{idx}: chip_path not found in sysfs "
                        f"(is virtrtlab_gpio loaded?)",
                        exit_code=4,
                    )
                try:
                    chip_path = chip_path_file.read_text().strip()
                except OSError as exc:
                    raise VirtrtlabError(
                        f"gpio{idx}: cannot read chip_path: {exc}",
                        exit_code=4,
                    ) from exc
                if not chip_path:
                    raise VirtrtlabError(
                        f"gpio{idx}: chip_path is empty in sysfs",
                        exit_code=4,
                    )

                entry: dict[str, Any] = {
                    "name": f"gpio{idx}",
                    "type": "gpio",
                    "chip_path": chip_path,
                    "control_path": ctrl,
                    "env": {
                        f"VIRTRTLAB_GPIOCHIP{idx}": chip_path,
                        f"VIRTRTLAB_GPIOCTRL{idx}": ctrl,
                    },
                }

                if sysfs_base_file.exists():
                    try:
                        base = int(sysfs_base_file.read_text().strip())
                        entry["sysfs_base"] = base
                        entry["env"][f"VIRTRTLAB_GPIOBASE{idx}"] = str(base)
                    except (OSError, ValueError):
                        warn_msg = (
                            f"gpio{idx}: sysfs_base unreadable, "
                            f"omitting VIRTRTLAB_GPIOBASE{idx}"
                        )
                        print(f"warning: {warn_msg}", file=sys.stderr)
                        entry.setdefault("warnings", []).append(warn_msg)
                else:
                    warn_msg = (
                        f"legacy sysfs GPIO ABI unavailable; "
                        f"VIRTRTLAB_GPIOBASE{idx} omitted"
                    )
                    print(f"warning: gpio{idx}: {warn_msg}", file=sys.stderr)
                    entry.setdefault("warnings", []).append(warn_msg)

                contract.append(entry)
            gpio_idx += count

    return contract


def _print_contract_human(contract: list[dict[str, Any]]) -> None:
    """Print the AUT integration contract in the spec-mandated human format.

    Env var emission order matches device-contract.md exactly:
      UART  : VIRTRTLAB_UART<N>
      GPIO  : VIRTRTLAB_GPIOCHIP<N>, VIRTRTLAB_GPIOBASE<N> (if present), VIRTRTLAB_GPIOCTRL<N>
    """
    for entry in contract:
        name = entry["name"]
        etype = entry["type"]
        idx = name[len(etype) :]  # e.g. "0" from "uart0" or "gpio0"
        env = entry.get("env", {})
        print(f"[ok] {name} loaded")
        if etype == "uart":
            print(f"     tty: {entry['aut_path']}")
            ordered_keys = [f"VIRTRTLAB_UART{idx}"]
        elif etype == "gpio":
            print(f"     gpiochip: {entry['chip_path']}")
            if "sysfs_base" in entry:
                print(f"     sysfs base: {entry['sysfs_base']}")
            print(f"     control: {entry['control_path']}")
            ordered_keys = [
                f"VIRTRTLAB_GPIOCHIP{idx}",
                f"VIRTRTLAB_GPIOBASE{idx}",
                f"VIRTRTLAB_GPIOCTRL{idx}",
            ]
        else:
            ordered_keys = []
        # Emit known keys in canonical order, then any extra keys sorted.
        emitted = set()
        for key in ordered_keys:
            if key in env:
                print(f"     export {key}={env[key]}")
                emitted.add(key)
        for key in sorted(k for k in env if k not in emitted):
            print(f"     export {key}={env[key]}")
        for warning in entry.get("warnings", []):
            print(f"     [warn] {warning}")
        print()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_up(args: argparse.Namespace) -> int:
    profile = _resolve_profile(args)
    no_sudo: bool = args.no_sudo
    module_dir: str | None = profile["build"].get("module_dir")

    # Idempotence: all expected modules loaded + daemon alive → warn and exit 0
    expected_module_names = _modules_load_order(profile)
    if (
        all(_is_module_loaded(m) for m in expected_module_names)
        and _daemon_pid() is not None
    ):
        print(
            "warning: lab already up (modules loaded, daemon running)", file=sys.stderr
        )
        contract = _resolve_aut_contract(profile)
        if args.json:
            _emit({"devices": contract}, True)
        else:
            _print_contract_human(contract)
        return 0

    _ensure_run_dir(no_sudo)

    # Build ordered load list: core first, then device modules
    modules_to_load: list[tuple[str, str, int]] = [("virtrtlab_core", "", 0)]
    for dev in profile["devices"]:
        ko_filename, param_name = MODULE_MAP[dev["type"]]
        modules_to_load.append(
            (ko_filename.removesuffix(".ko"), param_name, dev["count"])
        )

    loaded_this_run: list[str] = []
    for module_name, param_name, count in modules_to_load:
        if _is_module_loaded(module_name):
            continue
        ko_path = _find_ko(module_name, module_dir)
        try:
            _insmod(ko_path, param_name, count, no_sudo)
            loaded_this_run.append(module_name)
        except VirtrtlabError:
            for m in reversed(loaded_this_run):
                _rmmod(m, no_sudo, ignore_error=True)
            raise

    # Write bus seed if specified in profile
    seed = profile["bus"].get("seed")
    if seed is not None:
        try:
            subprocess.run(
                _sudo_prefix(no_sudo) + ["tee", f"{SYSFS_ROOT}/buses/vrtlbus0/seed"],
                input=str(seed),
                text=True,
                stdout=subprocess.DEVNULL,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            raise VirtrtlabError(
                f"failed to write bus seed: tee exited {exc.returncode}",
                exit_code=4,
            ) from exc

    # Persist load order so that `down` can reverse it
    all_mod_order = [m for m in expected_module_names if _is_module_loaded(m)]
    _write_run_file("modules.list", "\n".join(all_mod_order) + "\n", no_sudo)

    # Start daemon in background if not already running
    if _daemon_pid() is None:
        uart_count = sum(d["count"] for d in profile["devices"] if d["type"] == "uart")
        _launch_daemon(no_sudo, uart_count, RUN_DIR)

    # Poll all expected sockets (up to 5 s)
    expected_socks = _expected_sockets(profile)
    if expected_socks:
        _poll_sockets(expected_socks)
        # Socket permissions are set by the daemon at bind() time via
        # umask(0117) + chown(root:virtrtlab) on the socket path — no CLI
        # intervention needed.

    # GPIO inject and /dev/gpiochipN permissions are handled by udev rules
    # installed at /lib/udev/rules.d/90-virtrtlab.rules — no CLI intervention.

    # Resolve and emit the AUT integration contract
    contract = _resolve_aut_contract(profile)
    if args.json:
        _emit({"devices": contract}, True)
    else:
        _print_contract_human(contract)
    return 0


def cmd_down(args: argparse.Namespace) -> int:
    no_sudo: bool = args.no_sudo
    modules_list_file = Path(RUN_DIR) / "modules.list"

    if modules_list_file.exists():
        modules = [
            m.strip() for m in modules_list_file.read_text().splitlines() if m.strip()
        ]
    else:
        print(
            f"warning: {modules_list_file} not found — attempting rmmod on known modules",
            file=sys.stderr,
        )
        modules = list(KNOWN_MODULES)

    _stop_daemon(no_sudo, RUN_DIR)

    for module in reversed(modules):
        if _is_module_loaded(module):
            _rmmod(module, no_sudo, ignore_error=True)

    # Best-effort cleanup of state files
    prefix = _sudo_prefix(no_sudo)
    if modules_list_file.exists():
        subprocess.run(prefix + ["rm", "-f", str(modules_list_file)], check=False)

    return 0


def cmd_status(args: argparse.Namespace) -> int:
    # Modules
    module_info: dict[str, str] = {
        name: "loaded" if _is_module_loaded(name) else "not loaded"
        for name in KNOWN_MODULES
    }

    # Daemon
    pid = _daemon_pid()
    daemon_info: dict[str, Any] = (
        {"state": "running", "pid": pid} if pid else {"state": "stopped", "pid": None}
    )

    # Sockets
    run = Path(RUN_DIR)
    sockets = sorted(str(p) for p in run.glob("*.sock")) if run.exists() else []

    # Bus state
    bus_state_path = Path(SYSFS_ROOT) / "buses" / "vrtlbus0" / "state"
    bus_state = (
        bus_state_path.read_text().strip() if bus_state_path.exists() else "unknown"
    )

    if args.json:
        _emit(
            {
                "modules": module_info,
                "daemon": daemon_info,
                "sockets": sockets,
                "bus": {"vrtlbus0": {"state": bus_state}},
            },
            True,
        )
    else:
        print("modules:")
        for name, state in module_info.items():
            print(f"  {name:<25} {state}")
        print()
        print("daemon:")
        if pid:
            print(f"  pid    {pid}")
            print("  state  running")
        else:
            print("  state  stopped")
        print()
        print("sockets:")
        for sock in sockets:
            print(f"  {sock}")
        if not sockets:
            print("  (none)")
        print()
        print("bus vrtlbus0:")
        print(f"  state  {bus_state}")

    return 0


def cmd_list(args: argparse.Namespace) -> int:
    target = args.list_target  # "buses" or "devices"

    if target == "buses":
        buses_dir = Path(SYSFS_ROOT) / "buses"
        if not buses_dir.exists():
            raise VirtrtlabError(
                "sysfs bus directory not found (is the module loaded?)", exit_code=4
            )
        buses = sorted(p.name for p in buses_dir.iterdir() if p.is_dir())
        if args.json:
            _emit({"buses": buses}, True)
        else:
            for b in buses:
                print(b)
        return 0

    # target == "devices"
    devices_dir = Path(SYSFS_ROOT) / "devices"
    if not devices_dir.exists():
        raise VirtrtlabError(
            "sysfs device directory not found (is the module loaded?)", exit_code=4
        )
    type_filter: str | None = getattr(args, "type", None)
    result = []
    for dev_path in sorted(devices_dir.iterdir()):
        if not dev_path.is_dir():
            continue
        type_file = dev_path / "type"
        dev_type = type_file.read_text().strip() if type_file.exists() else "unknown"
        if type_filter and dev_type != type_filter:
            continue
        bus_file = dev_path / "bus"
        bus = bus_file.read_text().strip() if bus_file.exists() else "unknown"
        enabled_file = dev_path / "enabled"
        enabled = (
            enabled_file.read_text().strip() == "1" if enabled_file.exists() else None
        )
        result.append(
            {"name": dev_path.name, "type": dev_type, "bus": bus, "enabled": enabled}
        )

    if args.json:
        _emit({"devices": result}, True)
    else:
        for d in result:
            enabled_str = (
                ("yes" if d["enabled"] else "no") if d["enabled"] is not None else "?"
            )
            print(
                f"{d['name']:<20} type={d['type']:<8} bus={d['bus']:<12} enabled={enabled_str}"
            )
    return 0


def _sysfs_path(target: str, attr: str) -> Path:
    if target == "bus":
        return Path(SYSFS_ROOT) / "buses" / "vrtlbus0" / attr
    return Path(SYSFS_ROOT) / "devices" / target / attr


def cmd_get(args: argparse.Namespace) -> int:
    path = _sysfs_path(args.target, args.attr)
    if not path.exists():
        raise VirtrtlabError(f"attribute not found: {path}", exit_code=4)
    try:
        value = path.read_text().strip()
    except OSError as exc:
        raise VirtrtlabError(str(exc), exit_code=4) from exc
    if args.json:
        _emit({"target": args.target, "attr": args.attr, "value": value}, True)
    else:
        print(value)
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    for assignment in args.assignments:
        if "=" not in assignment:
            raise VirtrtlabError(
                f"invalid assignment '{assignment}': expected attr=value", exit_code=2
            )
        attr, _, value = assignment.partition("=")
        attr = attr.strip()
        if not attr:
            raise VirtrtlabError(
                f"invalid assignment '{assignment}': attribute name is empty",
                exit_code=2,
            )
        path = _sysfs_path(args.target, attr)
        if not path.exists():
            raise VirtrtlabError(f"attribute not found: {path}", exit_code=4)
        try:
            path.write_text(value)
        except OSError as exc:
            raise VirtrtlabError(
                f"kernel rejected write to {attr}: {exc.strerror}", exit_code=4
            ) from exc
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    stats_dir = Path(SYSFS_ROOT) / "devices" / args.device / "stats"
    if not stats_dir.exists():
        raise VirtrtlabError(f"stats directory not found: {stats_dir}", exit_code=4)
    stats: dict[str, "int | str"] = {}
    for f in sorted(stats_dir.iterdir()):
        if f.is_file():
            try:
                raw = f.read_text().strip()
                try:
                    stats[f.name] = int(raw)
                except ValueError:
                    stats[f.name] = raw
            except OSError:
                stats[f.name] = "<error>"

    if args.json:
        _emit({"device": args.device, "stats": stats}, True)
    else:
        print(f"{args.device} stats:")
        for k, v in stats.items():
            print(f"  {k:<20} {v}")
    return 0


def cmd_reset(args: argparse.Namespace) -> int:
    reset_path = Path(SYSFS_ROOT) / "devices" / args.device / "stats" / "reset"
    if not reset_path.exists():
        raise VirtrtlabError(f"reset file not found: {reset_path}", exit_code=4)
    try:
        reset_path.write_text("0")
    except OSError as exc:
        raise VirtrtlabError(
            f"kernel rejected stats reset for {args.device}: {exc.strerror}",
            exit_code=4,
        ) from exc
    return 0


def _valid_line(s: str) -> int:
    try:
        v = int(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"line must be an integer, got {s!r}")
    if not 0 <= v <= 7:
        raise argparse.ArgumentTypeError(f"line must be 0..7, got {v}")
    return v


def _valid_value(s: str) -> int:
    try:
        v = int(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"value must be 0 or 1, got {s!r}")
    if v not in (0, 1):
        raise argparse.ArgumentTypeError(f"value must be 0 or 1, got {v}")
    return v


def cmd_inject(args: argparse.Namespace) -> int:
    device: str = args.device
    line: int = args.line
    value: int = args.value

    device_path = Path(SYSFS_ROOT) / "devices" / device
    if not device_path.is_dir():
        raise VirtrtlabError(
            f"device not found: {device} (is the module loaded?)", exit_code=4
        )

    inject_path = device_path / "inject"
    if not inject_path.exists():
        raise VirtrtlabError(
            f"{device} does not support injection (no 'inject' attribute)", exit_code=4
        )

    try:
        inject_path.write_text(f"{line}:{value}")
    except OSError as exc:
        raise VirtrtlabError(
            f"kernel rejected inject on {device} line {line}: {exc.strerror}",
            exit_code=4,
        ) from exc

    if args.json:
        _emit({"device": device, "line": line, "value": value, "status": "ok"}, True)
    else:
        print(f"{device} line {line} ← {value}")
    return 0


def cmd_daemon(args: argparse.Namespace) -> int:
    subcmd: str = args.daemon_command
    no_sudo: bool = args.no_sudo

    if subcmd == "status":
        pid = _daemon_pid()
        if args.json:
            _emit(
                {"state": "running", "pid": pid}
                if pid
                else {"state": "stopped", "pid": None},
                True,
            )
        else:
            if pid:
                print(f"state  running\npid    {pid}")
            else:
                print("state  stopped")
        return 0 if pid else 3

    if subcmd == "stop":
        _stop_daemon(no_sudo)
        return 0

    # subcmd == "start"
    if _daemon_pid() is not None:
        raise VirtrtlabError("daemon is already running", exit_code=3)

    num_uarts: int = args.num_uarts
    run_dir: str = args.run_dir

    _run_cmd(_sudo_prefix(no_sudo) + ["mkdir", "-p", run_dir], exit_code=1)
    actual_pid = _launch_daemon(no_sudo, num_uarts, run_dir)

    # Poll uart sockets in the correct run_dir
    expected_socks = [Path(run_dir) / f"uart{i}.sock" for i in range(num_uarts)]
    _poll_sockets(expected_socks)

    if actual_pid is None:
        actual_pid = _daemon_pid(run_dir) or _find_virtrtlabd_pid()
    if actual_pid is None:
        raise VirtrtlabError(
            "daemon started but PID could not be determined", exit_code=3
        )

    if args.json:
        _emit({"state": "running", "pid": actual_pid}, True)
    else:
        print(f"daemon started (pid {actual_pid})")
    return 0


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="virtrtlabctl",
        description="VirtRTLab control CLI",
    )
    parser.add_argument(
        "--json", action="store_true", help="Machine-readable JSON output"
    )
    parser.add_argument(
        "--no-sudo",
        action="store_true",
        help="Do not prepend sudo to privileged operations",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # up
    p_up = sub.add_parser("up", help="Bring up a lab profile")
    p_up.add_argument("--config", metavar="FILE", help="Path to TOML lab profile")
    p_up.add_argument("--uart", type=int, metavar="N", help="Number of UART instances")
    p_up.add_argument("--gpio", type=int, metavar="N", help="Number of GPIO instances")
    p_up.set_defaults(func=cmd_up)

    # down
    p_down = sub.add_parser("down", help="Tear down a lab")
    p_down.set_defaults(func=cmd_down)

    # status
    p_status = sub.add_parser("status", help="Global lab status")
    p_status.set_defaults(func=cmd_status)

    # list
    p_list = sub.add_parser("list", help="Discover buses and devices")
    list_sub = p_list.add_subparsers(dest="list_target", required=True)
    list_sub.add_parser("buses", help="List virtual buses")
    p_list_dev = list_sub.add_parser("devices", help="List virtual devices")
    p_list_dev.add_argument("--type", metavar="TYPE", help="Filter by device type")
    p_list.set_defaults(func=cmd_list)

    # get
    p_get = sub.add_parser("get", help="Read a sysfs attribute")
    p_get.add_argument("target", help="Device name or 'bus'")
    p_get.add_argument("attr", help="Attribute name")
    p_get.set_defaults(func=cmd_get)

    # set
    p_set = sub.add_parser("set", help="Write sysfs attribute(s)")
    p_set.add_argument("target", help="Device name or 'bus'")
    p_set.add_argument(
        "assignments", nargs="+", metavar="attr=value", help="Attribute assignments"
    )
    p_set.set_defaults(func=cmd_set)

    # stats
    p_stats = sub.add_parser("stats", help="Display per-device counters")
    p_stats.add_argument("device", help="Device name")
    p_stats.set_defaults(func=cmd_stats)

    # reset
    p_reset = sub.add_parser("reset", help="Reset stats counters")
    p_reset.add_argument("device", help="Device name")
    p_reset.set_defaults(func=cmd_reset)

    # inject
    p_inject = sub.add_parser("inject", help="Inject a GPIO line value")
    p_inject.add_argument("device", help="GPIO device name (e.g. gpio0)")
    p_inject.add_argument(
        "line",
        type=_valid_line,
        metavar="LINE",
        help="GPIO line index (0..7)",
    )
    p_inject.add_argument(
        "value",
        type=_valid_value,
        metavar="VALUE",
        help="Physical value to inject (0 or 1)",
    )
    p_inject.set_defaults(func=cmd_inject)

    # daemon
    p_daemon = sub.add_parser("daemon", help="Manage virtrtlabd independently")
    daemon_sub = p_daemon.add_subparsers(dest="daemon_command", required=True)
    p_dstart = daemon_sub.add_parser("start", help="Start the daemon")
    p_dstart.add_argument("--num-uarts", type=int, metavar="N", default=1)
    p_dstart.add_argument("--run-dir", metavar="DIR", default=RUN_DIR)
    daemon_sub.add_parser("stop", help="Stop the daemon")
    daemon_sub.add_parser("status", help="Daemon status")
    p_daemon.set_defaults(func=cmd_daemon)

    # internal
    p_internal = sub.add_parser("__spawn-daemon-detached", help=argparse.SUPPRESS)
    p_internal.add_argument("--num-uarts", type=int, metavar="N", required=True)
    p_internal.add_argument("--run-dir", metavar="DIR", required=True)
    p_internal.add_argument("--pid-file", metavar="FILE", required=True)
    p_internal.add_argument("--daemon-bin", metavar="PATH", required=True)
    p_internal.set_defaults(func=cmd_spawn_daemon_detached)

    return parser


def cmd_spawn_daemon_detached(args: argparse.Namespace) -> int:
    try:
        proc = _spawn_daemon_detached(
            args.num_uarts,
            args.run_dir,
            daemon_bin=args.daemon_bin,
        )
        Path(args.pid_file).write_text(str(proc.pid) + "\n")
        return 0
    except OSError as exc:
        raise VirtrtlabError(
            f"failed to spawn detached daemon helper: {exc.strerror or exc}",
            exit_code=1,
        ) from exc


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return cast(int, args.func(args))
    except VirtrtlabError as exc:
        if getattr(args, "json", False):
            print(json.dumps({"error": str(exc), "code": exc.exit_code}))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    except NotImplementedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
