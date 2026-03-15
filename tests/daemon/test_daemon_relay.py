"""
test_daemon_relay.py — byte relay tests for virtrtlabd (issue #10).

Acceptance criteria covered:
  AC2 — bytes flow between /dev/ttyVIRTLAB0 and /run/virtrtlab/uart0.sock

Data path:
  AUT→sim : write(tty_fd) → virtrtlab_uart TX → wire device → daemon → socket
  sim→AUT : socket → daemon → write(wire_fd) → virtrtlab_uart RX → read(tty_fd)
"""

import os
import select
import socket

import pytest

from conftest import SOCK_PATH, open_tty_raw, restore_tty


class TestDaemonRelay:
    """Bytes flow correctly in both directions through the daemon relay."""

    def test_bytes_aut_to_simulator(self, daemon_proc):
        """
        Bytes written to /dev/ttyVIRTLAB0 (AUT side) must appear on the
        simulator socket within 1 s.
        """
        payload = b"Hello"
        tty_fd, saved = open_tty_raw()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(SOCK_PATH)
            sock.setblocking(False)

            os.write(tty_fd, payload)

            r, _, _ = select.select([sock], [], [], 1.0)
            assert r, "Socket not readable within 1 s after TTY write"

            data = b""
            while len(data) < len(payload):
                r, _, _ = select.select([sock], [], [], 0.5)
                if not r:
                    break
                chunk = sock.recv(256)
                if not chunk:
                    break
                data += chunk

            assert data == payload, f"Expected {payload!r}, got {data!r}"

        finally:
            sock.close()
            restore_tty(tty_fd, saved)

    def test_bytes_simulator_to_aut(self, daemon_proc):
        """
        Bytes sent on the simulator socket must appear on /dev/ttyVIRTLAB0
        (AUT side) within 1 s.
        """
        payload = b"World"
        tty_fd, saved = open_tty_raw()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(SOCK_PATH)

            sock.sendall(payload)

            r, _, _ = select.select([tty_fd], [], [], 1.0)
            assert r, "TTY not readable within 1 s after socket write"

            data = b""
            while len(data) < len(payload):
                r, _, _ = select.select([tty_fd], [], [], 0.5)
                if not r:
                    break
                chunk = os.read(tty_fd, 256)
                if not chunk:
                    break
                data += chunk

            assert data == payload, f"Expected {payload!r}, got {data!r}"

        finally:
            sock.close()
            restore_tty(tty_fd, saved)
