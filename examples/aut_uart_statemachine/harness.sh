#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
#
# harness.sh — fault-injection harness for aut_uart_statemachine
#
# Scenario
# --------
#   The AUT implements a 3-state machine (IDLE → GOT_CMD → GOT_DATA) that
#   reads a 3-byte frame: [SOF=0x01, data=0x42, checksum=0x43].
#   The bug: no RESET state.  If an extra byte is injected between the data
#   byte and the checksum byte, the state machine consumes it as the checksum,
#   fails the XOR check, and exits 2 — with no way to recover.
#
#   Baseline: harness sends exact frame [0x01, 0x42, 0x43] → AUT exits 0.
#
#   Fault:    harness injects a retransmit byte (0xFF) before the real
#             checksum: [0x01, 0x42, 0xFF, 0x43].
#             The AUT reads 0xFF as the checksum, computes expected=0x43,
#             detects mismatch, and exits 2.
#
# Usage
# -----
#   harness.sh [--baseline]
#
#   Without arguments: fault mode — exits non-zero (harness PASS).
#   With --baseline:   baseline mode — exits 0 (harness PASS).
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
AUT_TIMEOUT=6
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
done < <(virtrtlabctl up --uart 1 2>/dev/null | grep -oE 'VIRTRTLAB_[A-Z0-9]+=[^ ]+')

if [[ -z "${VIRTRTLAB_UART0:-}" ]]; then
    echo "FAIL: virtrtlabctl up --uart 1 did not export VIRTRTLAB_UART0"
    exit 1
fi

if [[ ! -S "$SOCK" ]]; then
    echo "FAIL: daemon socket $SOCK not found — is virtrtlabd running?"
    exit 1
fi

# ---------------------------------------------------------------------------
# AUT runs in the background so the simulator (foreground heredoc) can read
# the READY byte after the AUT has already opened the TTY.
# Same rule as uart_timeout: never use "|| true" after "wait $AUT_PID".
# ---------------------------------------------------------------------------
timeout "$AUT_TIMEOUT" "$AUT" &
AUT_PID=$!

# Give the AUT time to open the tty and send its READY byte (0x55).
sleep 0.3

# ---------------------------------------------------------------------------
# Act as the simulator: read READY byte, then send the frame.
#
#   Baseline: [0x01, 0x42, 0x43]              — clean frame, checksum OK.
#   Fault:    [0x01, 0x42, 0xFF, 0x43]        — 0xFF injected as retransmit;
#             AUT consumes 0xFF as checksum → mismatch → exits 2.
# ---------------------------------------------------------------------------
FAULT="$FAULT_MODE" python3 - "$SOCK" <<'PYEOF'
import os, socket, sys, time

sock_path = sys.argv[1]
fault     = os.environ.get("FAULT", "false").lower() == "true"

SOF      = 0x01
DATA     = 0x42
CHECKSUM = SOF ^ DATA   # = 0x43
INJECT   = 0xFF         # extra byte simulating a retransmit

with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
    s.settimeout(5.0)
    try:
        s.connect(sock_path)
    except OSError as e:
        print(f"FAIL: cannot connect to daemon socket: {e}", file=sys.stderr)
        sys.exit(1)

    # Read the READY byte from the AUT.
    try:
        data = s.recv(1)
    except socket.timeout:
        print("FAIL: timeout waiting for READY byte from AUT", file=sys.stderr)
        sys.exit(1)

    if not data or data[0] != 0x55:
        print(f"FAIL: expected READY byte 0x55, got {data!r}", file=sys.stderr)
        sys.exit(1)

    if fault:
        # Inject retransmit byte between data and checksum.
        frame = bytes([SOF, DATA, INJECT, CHECKSUM])
    else:
        frame = bytes([SOF, DATA, CHECKSUM])

    s.sendall(frame)
    time.sleep(3.0)
PYEOF

# Collect the AUT exit code (same set -e-safe pattern as uart_timeout).
AUT_RC=0
wait "$AUT_PID" || AUT_RC=$?

if $FAULT_MODE; then
    # AUT must exit non-zero (2 = BAD STATE) because of the injected byte.
    if [[ "$AUT_RC" -ne 0 ]]; then
        echo "PASS: state machine entered bad state as expected (exit $AUT_RC)"
        exit "$AUT_RC"   # non-zero — fault was injected
    else
        echo "FAIL: AUT exited 0 despite injected retransmit — bug not triggered"
        exit 1
    fi
else
    if [[ "$AUT_RC" -eq 0 ]]; then
        echo "PASS: state machine completed correctly in baseline mode"
        exit 0
    else
        echo "FAIL: state machine failed in baseline mode (exit $AUT_RC)"
        exit "$AUT_RC"
    fi
fi
