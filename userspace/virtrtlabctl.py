#!/usr/bin/env python3

import argparse
import json
import os
import socket
import sys

DEFAULT_SOCKET = "/run/virtrtlab.sock"


def _connect(sock_path: str) -> socket.socket:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(sock_path)
    return client


def cmd_send(args: argparse.Namespace) -> int:
    sock_path = args.socket
    if not os.path.exists(sock_path):
        sys.stderr.write(f"Socket not found: {sock_path}\n")
        return 3

    try:
        json.loads(args.json)
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"Invalid JSON: {exc}\n")
        return 2

    client = _connect(sock_path)
    with client:
        client.sendall((args.json + "\n").encode("utf-8"))
        if args.no_read:
            return 0
        data = client.recv(1024 * 1024)
        sys.stdout.write(data.decode("utf-8", errors="replace"))
        return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="virtrtlabctl")
    parser.add_argument("--socket", default=DEFAULT_SOCKET, help="UNIX socket path")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_send = sub.add_parser("send", help="Send a raw JSON object (as a single line)")
    p_send.add_argument("json", help="JSON object string")
    p_send.add_argument("--no-read", action="store_true", help="Do not wait for response")
    p_send.set_defaults(func=cmd_send)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
