#!/usr/bin/env python3
"""virtrtlabctl — VirtRTLab control CLI."""

import argparse
import json
import subprocess
import sys
import time
import tomllib
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SYSFS_ROOT = "/sys/kernel/virtrtlab"
RUN_DIR = "/run/virtrtlab"
DAEMON_BIN = "virtrtlabd"
KNOWN_MODULES = ["virtrtlab_core", "virtrtlab_uart", "virtrtlab_gpio"]

# type → (ko filename, insmod parameter name)
MODULE_MAP: dict[str, tuple[str, str]] = {
    "uart": ("virtrtlab_uart.ko", "num_uarts"),
    "gpio": ("virtrtlab_gpio.ko", "num_gpio"),
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
    return [] if no_sudo else ["sudo"]


def _run_cmd(
    cmd: list[str],
    *,
    check: bool = True,
    capture: bool = False,
    exit_code: int = 1,
) -> subprocess.CompletedProcess:
    kwargs: dict = {}
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


def _emit(data: "dict | list | str", json_flag: bool) -> None:
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

    Search order: module_dir (if given) → ./ → /lib/modules/$(uname -r)/
    """
    filename = f"{module_name}.ko"
    candidates: list[Path] = []
    if module_dir:
        candidates.append(Path(module_dir) / filename)
    candidates.append(Path(".") / filename)
    try:
        uname_r = subprocess.check_output(["uname", "-r"], text=True).strip()
        candidates.append(Path(f"/lib/modules/{uname_r}") / filename)
    except subprocess.SubprocessError:
        pass
    for path in candidates:
        if path.exists():
            return path
    searched = ", ".join(str(p) for p in candidates)
    raise VirtrtlabError(f"{filename} not found (searched: {searched})", exit_code=1)


def _resolve_profile(args: argparse.Namespace) -> dict:
    """Resolve a lab profile from TOML file + inline overrides.

    Returns: {'devices': [{'type': str, 'count': int}, ...],
               'bus':     {...},
               'build':   {'module_dir': str | None, ...}}
    Raises VirtrtlabError(2) on parse/validation errors.
    """
    profile: dict = {"devices": [], "bus": {}, "build": {}}

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
                raise VirtrtlabError(f"profile parse error: {exc}", exit_code=2) from exc
        profile["build"] = data.get("build", {})
        profile["bus"] = data.get("bus", {})
        profile["devices"] = [
            {"type": d["type"], "count": int(d.get("count", 1))}
            for d in data.get("devices", [])
        ]

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
            raise VirtrtlabError(
                f"unknown device type: {dev['type']}", exit_code=2
            )

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


def _daemon_pid() -> int | None:
    """Return daemon PID if the process is alive, else None."""
    pid_file = Path(RUN_DIR) / "daemon.pid"
    try:
        pid = int(pid_file.read_text().strip())
        if Path(f"/proc/{pid}").exists():
            return pid
    except (OSError, ValueError):
        pass
    return None


def _ensure_run_dir(no_sudo: bool) -> None:
    _run_cmd(_sudo_prefix(no_sudo) + ["mkdir", "-p", RUN_DIR], exit_code=1)


def _write_run_file(filename: str, content: str, no_sudo: bool) -> None:
    """Write content to RUN_DIR/<filename> via sudo tee (or directly if --no-sudo)."""
    path = Path(RUN_DIR) / filename
    if no_sudo:
        path.write_text(content)
    else:
        subprocess.run(
            ["sudo", "tee", str(path)],
            input=content, text=True,
            stdout=subprocess.DEVNULL, check=True,
        )


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


def _stop_daemon(no_sudo: bool) -> None:
    """SIGTERM the daemon, wait 5 s, SIGKILL if still alive."""
    pid = _daemon_pid()
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
    pid_file = Path(RUN_DIR) / "daemon.pid"
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
            raise VirtrtlabError(
                f"timeout waiting for sockets: {missing}", exit_code=3
            )
        time.sleep(0.1)


def _expected_sockets(profile: dict) -> list[Path]:
    """Return expected socket paths for UART devices in the profile."""
    run = Path(RUN_DIR)
    return [
        run / f"uart{i}.sock"
        for dev in profile["devices"]
        if dev["type"] == "uart"
        for i in range(dev["count"])
    ]


def _modules_load_order(profile: dict) -> list[str]:
    """Return module names in load order: core first, then device modules."""
    names: list[str] = ["virtrtlab_core"]
    for dev in profile["devices"]:
        name = MODULE_MAP[dev["type"]][0].removesuffix(".ko")
        if name not in names:
            names.append(name)
    return names


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
        print("warning: lab already up (modules loaded, daemon running)", file=sys.stderr)
        return 0

    _ensure_run_dir(no_sudo)

    # Build ordered load list: core first, then device modules
    modules_to_load: list[tuple[str, str, int]] = [("virtrtlab_core", "", 0)]
    for dev in profile["devices"]:
        ko_filename, param_name = MODULE_MAP[dev["type"]]
        modules_to_load.append((ko_filename.removesuffix(".ko"), param_name, dev["count"]))

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
        subprocess.run(
            _sudo_prefix(no_sudo) + ["tee", f"{SYSFS_ROOT}/buses/vrtlbus0/seed"],
            input=str(seed), text=True, stdout=subprocess.DEVNULL,
        )

    # Persist load order so that `down` can reverse it
    all_mod_order = [m for m in expected_module_names if _is_module_loaded(m)]
    _write_run_file("modules.list", "\n".join(all_mod_order) + "\n", no_sudo)

    # Start daemon in background if not already running
    if _daemon_pid() is None:
        uart_count = sum(d["count"] for d in profile["devices"] if d["type"] == "uart")
        subprocess.Popen(
            _sudo_prefix(no_sudo) + [DAEMON_BIN, "--num-uarts", str(uart_count), "--run-dir", RUN_DIR],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    # Poll all expected sockets (up to 5 s)
    expected_socks = _expected_sockets(profile)
    if expected_socks:
        _poll_sockets(expected_socks)

    if args.json:
        _emit(
            {"status": "up", "modules": all_mod_order, "sockets": [str(s) for s in expected_socks]},
            True,
        )
    else:
        print(f"lab up — modules: {', '.join(all_mod_order)}")
    return 0


def cmd_down(args: argparse.Namespace) -> int:
    no_sudo: bool = args.no_sudo
    modules_list_file = Path(RUN_DIR) / "modules.list"

    if modules_list_file.exists():
        modules = [m.strip() for m in modules_list_file.read_text().splitlines() if m.strip()]
    else:
        print(
            f"warning: {modules_list_file} not found — attempting rmmod on known modules",
            file=sys.stderr,
        )
        modules = list(KNOWN_MODULES)

    _stop_daemon(no_sudo)

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
    daemon_info: dict = (
        {"state": "running", "pid": pid} if pid else {"state": "stopped", "pid": None}
    )

    # Sockets
    run = Path(RUN_DIR)
    sockets = sorted(str(p) for p in run.glob("*.sock")) if run.exists() else []

    # Bus state
    bus_state_path = Path(SYSFS_ROOT) / "buses" / "vrtlbus0" / "state"
    bus_state = bus_state_path.read_text().strip() if bus_state_path.exists() else "unknown"

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
            print(f"  state  running")
        else:
            print(f"  state  stopped")
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
            raise VirtrtlabError("sysfs bus directory not found (is the module loaded?)", exit_code=4)
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
        raise VirtrtlabError("sysfs device directory not found (is the module loaded?)", exit_code=4)
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
        enabled = enabled_file.read_text().strip() == "1" if enabled_file.exists() else None
        result.append({"name": dev_path.name, "type": dev_type, "bus": bus, "enabled": enabled})

    if args.json:
        _emit({"devices": result}, True)
    else:
        for d in result:
            enabled_str = ("yes" if d["enabled"] else "no") if d["enabled"] is not None else "?"
            print(f"{d['name']:<20} type={d['type']:<8} bus={d['bus']:<12} enabled={enabled_str}")
    return 0


def _sysfs_path(target: str, attr: str) -> Path:
    if target == "bus":
        return Path(SYSFS_ROOT) / "buses" / "vrtlbus0" / attr
    return Path(SYSFS_ROOT) / "devices" / target / attr


def cmd_get(args: argparse.Namespace) -> int:
    path = _sysfs_path(args.target, args.attr)
    if not path.exists():
        raise VirtrtlabError(
            f"attribute not found: {path}", exit_code=4
        )
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
                f"invalid assignment '{assignment}': attribute name is empty", exit_code=2
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
    stats: dict[str, str] = {}
    for f in sorted(stats_dir.iterdir()):
        if f.is_file():
            try:
                stats[f.name] = f.read_text().strip()
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
            f"kernel rejected stats reset for {args.device}: {exc.strerror}", exit_code=4
        ) from exc
    return 0


def cmd_daemon(args: argparse.Namespace) -> int:
    subcmd: str = args.daemon_command
    no_sudo: bool = args.no_sudo

    if subcmd == "status":
        pid = _daemon_pid()
        if args.json:
            _emit(
                {"state": "running", "pid": pid} if pid else {"state": "stopped", "pid": None},
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

    _ensure_run_dir(no_sudo)
    proc = subprocess.Popen(
        _sudo_prefix(no_sudo) + [DAEMON_BIN, "--num-uarts", str(num_uarts), "--run-dir", run_dir],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    _write_run_file("daemon.pid", str(proc.pid) + "\n", no_sudo)

    # Poll uart sockets
    expected_socks = [Path(run_dir) / f"uart{i}.sock" for i in range(num_uarts)]
    _poll_sockets(expected_socks)

    if args.json:
        _emit({"state": "running", "pid": proc.pid}, True)
    else:
        print(f"daemon started (pid {proc.pid})")
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

    # daemon
    p_daemon = sub.add_parser("daemon", help="Manage virtrtlabd independently")
    daemon_sub = p_daemon.add_subparsers(dest="daemon_command", required=True)
    p_dstart = daemon_sub.add_parser("start", help="Start the daemon")
    p_dstart.add_argument("--num-uarts", type=int, metavar="N", default=1)
    p_dstart.add_argument("--run-dir", metavar="DIR", default=RUN_DIR)
    daemon_sub.add_parser("stop", help="Stop the daemon")
    daemon_sub.add_parser("status", help="Daemon status")
    p_daemon.set_defaults(func=cmd_daemon)

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
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
