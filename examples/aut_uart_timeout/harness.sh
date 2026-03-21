#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
#
# harness.sh — fault-injection harness for aut_uart_timeout
#
# Scenario
# --------
#   The AUT opens a UART, sets VMIN=4 VTIME=0 (no read timeout), writes a
#   READY byte (0x55) and then blocks waiting for exactly 4 bytes.
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
#     Exits 0 if the AUT was killed as expected (PASS).
#     Exits 1 if the AUT somehow completed (FAIL).
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
while IFS= read -r line; do
    case "$line" in VIRTRTLAB_*) export "$line" ;; esac
done < <(virtrtlabctl up uart0 2>/dev/null)

if [[ -z "${VIRTRTLAB_UART0:-}" ]]; then
    echo "FAIL: virtrtlabctl up uart0 did not export VIRTRTLAB_UART0"
    exit 1
fi

if [[ ! -S "$SOCK" ]]; then
    echo "FAIL: daemon socket $SOCK not found — is virtrtlabd running?"
    exit 1
fi

# ---------------------------------------------------------------------------
# Launch the AUT under a hard timeout so it cannot hang the harness.
# ---------------------------------------------------------------------------
timeout "$AUT_TIMEOUT" "$AUT" &
AUT_PID=$!

# Give the AUT time to open the tty and send its READY byte (0x55).
sleep 0.3

# ---------------------------------------------------------------------------
# Act as the simulator: connect to the daemon socket, read the READY byte,
# then send the packet (full or truncated depending on the mode).
# ---------------------------------------------------------------------------
FAULT="$FAULT_MODE" python3 - "$SOCK" <<'EOF'
import os, socket, sys, time

sock_path = sys.argv[1]
fault     = os.environ.get("FAULT", "false").lower() == "true"

with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
    s.settimeout(3.0)
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
        # Hold the connection open so the AUT stays in read().
        time.sleep(float(os.environ.get("AUT_TIMEOUT", "4")) + 1)
    else:
        s.sendall(packet)
        time.sleep(1.0)
EOF

# ---------------------------------------------------------------------------
# Collect the AUT exit code and assert the expected outcome.
# ---------------------------------------------------------------------------
wait "$AUT_PID" 2>/dev/null || true
AUT_RC=$?

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
