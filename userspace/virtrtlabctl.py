#!/usr/bin/env python3
"""virtrtlabctl — VirtRTLab control CLI."""

import argparse
import json
import subprocess
import sys

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SYSFS_ROOT = "/sys/kernel/virtrtlab"
RUN_DIR = "/run/virtrtlab"
DAEMON_BIN = "virtrtlabd"
KNOWN_MODULES = ["virtrtlab_core", "virtrtlab_uart", "virtrtlab_gpio"]

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
# Command stubs (implemented in T3–T6)
# ---------------------------------------------------------------------------


def cmd_up(args: argparse.Namespace) -> int:
    raise NotImplementedError("cmd_up not yet implemented")


def cmd_down(args: argparse.Namespace) -> int:
    raise NotImplementedError("cmd_down not yet implemented")


def cmd_status(args: argparse.Namespace) -> int:
    raise NotImplementedError("cmd_status not yet implemented")


def cmd_list(args: argparse.Namespace) -> int:
    raise NotImplementedError("cmd_list not yet implemented")


def cmd_get(args: argparse.Namespace) -> int:
    raise NotImplementedError("cmd_get not yet implemented")


def cmd_set(args: argparse.Namespace) -> int:
    raise NotImplementedError("cmd_set not yet implemented")


def cmd_stats(args: argparse.Namespace) -> int:
    raise NotImplementedError("cmd_stats not yet implemented")


def cmd_reset(args: argparse.Namespace) -> int:
    raise NotImplementedError("cmd_reset not yet implemented")


def cmd_daemon(args: argparse.Namespace) -> int:
    raise NotImplementedError("cmd_daemon not yet implemented")


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
