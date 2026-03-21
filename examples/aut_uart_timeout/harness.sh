#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
#
# harness.sh — fault-injection harness for aut_uart_timeout
#
# Scenario
# --------
#   The AUT opens a UART, sets VMIN=1 VTIME=0 (no read timeout), writes a
#   READY byte (0x55) and then reads 4 data bytes one by one, each blocking
#   until a byte is available.
#
#   Baseline: the harness (acting as the UART simulator) sends all 4 bytes.
#             The AUT verifies the checksum and exits 0.
#
#   Fault:    the harness sends only 3 bytes and keeps the connection open.
#             The AUT blocks forever on read() and is killed by timeout(1).
#
# Usage
# -----
#   harness.sh [--baseline]
#
#   Without arguments: fault mode.
#     Sends 3 of 4 bytes — the AUT hangs.
#     Exits non-zero (AUT exit code) when the AUT is killed as expected (PASS).
#     Exits 1 if the AUT somehow completed without fault (FAIL).
#
#   With --baseline: baseline mode.
#     Sends all 4 bytes — the AUT completes cleanly.
#     Exits 0 on success (PASS), non-zero on failure.
#
# Prerequisites
# -------------
#   - virtrtlab_core and virtrtlab_uart kernel modules loaded
#   - virtrtlabd running
#   - virtrtlabctl in PATH
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AUT="$SCRIPT_DIR/aut"
AUT_TIMEOUT=4      # seconds before we consider the AUT hung
FAULT_MODE=true
SOCK="/run/virtrtlab/uart0.sock"

if [[ "${1:-}" == "--baseline" ]]; then
    FAULT_MODE=false
fi

if [[ ! -x "$AUT" ]]; then
    echo "FAIL: AUT binary not found — run 'make -C examples' first"
    exit 1
fi

# ---------------------------------------------------------------------------
# Load the UART device and export env vars.
# ---------------------------------------------------------------------------
while IFS= read -r kv; do
    export "$kv"
done < <(virtrtlabctl up --uart 1 | grep -oE 'VIRTRTLAB_[A-Z0-9]+=[^ ]+')

if [[ -z "${VIRTRTLAB_UART0:-}" ]]; then
    echo "FAIL: virtrtlabctl up --uart 1 did not export VIRTRTLAB_UART0"
    exit 1
fi

if [[ ! -S "$SOCK" ]]; then
    echo "FAIL: daemon socket $SOCK not found — is virtrtlabd running?"
    exit 1
fi

# ---------------------------------------------------------------------------
# AUT runs in the background so the simulator can run synchronously
# (foreground heredoc).  This ensures the AUT has time to open the TTY
# before the simulator connects — necessary because the virtual UART driver
# raises HUP on wire_fd when no AUT process holds the device open.
#
# KEY: do NOT use "|| true" after "wait $AUT_PID" — that would discard the
# 124 exit code that timeout(1) returns when it kills the child.
# ---------------------------------------------------------------------------
timeout "$AUT_TIMEOUT" "$AUT" &
AUT_PID=$!

# Give the AUT time to open the tty and send its READY byte (0x55).
sleep 0.3

# ---------------------------------------------------------------------------
# Act as the simulator: connect to the daemon socket, read the READY byte,
# then send the packet (full or truncated depending on the mode).
# In fault mode we sleep AUT_TIMEOUT+2 to keep the connection open until
# timeout(1) kills the AUT; only then do we exit and free the socket.
# ---------------------------------------------------------------------------
FAULT="$FAULT_MODE" AUT_TIMEOUT="$AUT_TIMEOUT" python3 - "$SOCK" <<'PYEOF'
import os, socket, sys, time

sock_path = sys.argv[1]
fault     = os.environ.get("FAULT", "false").lower() == "true"
timeout_s = float(os.environ.get("AUT_TIMEOUT", "4"))

with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
    s.settimeout(timeout_s)
    try:
        s.connect(sock_path)
    except OSError as e:
        print(f"FAIL: cannot connect to daemon socket: {e}", file=sys.stderr)
        sys.exit(1)

    # Read the READY byte that the AUT sent.
    try:
        data = s.recv(1)
    except socket.timeout:
        print("FAIL: timeout waiting for READY byte from AUT", file=sys.stderr)
        sys.exit(1)

    if not data or data[0] != 0x55:
        print(f"FAIL: expected READY byte 0x55, got {data!r}", file=sys.stderr)
        sys.exit(1)

    # Build a valid 4-byte packet: [0xAA, 0xBB, 0xCC, checksum].
    payload = bytes([0xAA, 0xBB, 0xCC])
    cksum   = payload[0] ^ payload[1] ^ payload[2]
    packet  = payload + bytes([cksum])

    if fault:
        # Send only 3 bytes — the AUT will block waiting for the 4th.
        s.sendall(packet[:3])
        # Hold the connection open until timeout(1) kills the AUT.
        time.sleep(timeout_s + 2)
    else:
        s.sendall(packet)
        time.sleep(2.0)
PYEOF

# ---------------------------------------------------------------------------
# Collect the AUT exit code.
# With "set -e", a bare "wait PID" returning 124 would abort the script
# before we can inspect $?.  The "|| AUT_RC=$?" pattern captures the non-zero
# exit status from the left side without tripping set -e.
# ---------------------------------------------------------------------------
AUT_RC=0
wait "$AUT_PID" || AUT_RC=$?

if $FAULT_MODE; then
    # timeout(1) exits 124 when it kills the child; any non-zero is fine.
    if [[ "$AUT_RC" -ne 0 ]]; then
        echo "PASS: AUT hung as expected under fault injection (exit $AUT_RC)"
        exit "$AUT_RC"   # non-zero — fault was injected
    else
        echo "FAIL: AUT exited 0 despite missing byte — bug not triggered"
        exit 1
    fi
else
    if [[ "$AUT_RC" -eq 0 ]]; then
        echo "PASS: AUT completed successfully in baseline mode"
        exit 0
    else
        echo "FAIL: AUT failed in baseline mode (exit $AUT_RC)"
        exit "$AUT_RC"
    fi
fi
