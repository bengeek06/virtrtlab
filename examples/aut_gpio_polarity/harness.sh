#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
#
# harness.sh — fault-injection harness for aut_gpio_polarity
#
# Scenario
# --------
#   The AUT opens /dev/gpiochipN, requests line 1 as input with RISING edge
#   detection (that is the bug — it should watch FALLING), then waits up to
#   3 seconds for an event.
#
#   Baseline: harness pre-sets line 1 low (0), starts AUT, then injects a
#             rising edge (0 → 1).  AUT sees the event and exits 0.
#
#   Fault:    harness pre-sets line 1 high (1), starts AUT, then injects a
#             falling edge (1 → 0).  AUT watches only rising edges and never
#             sees the event; it times out and exits 1.
#             The harness asserts this non-zero exit as the expected outcome.
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
#   - virtrtlab_core and virtrtlab_gpio kernel modules loaded
#   - virtrtlabctl in PATH
#   - CONFIG_GPIO_CDEV enabled (Linux ≥ 5.10)
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AUT="$SCRIPT_DIR/aut"
AUT_TIMEOUT=5      # slightly longer than AUT's own 3-second poll timeout
FAULT_MODE=true

if [[ "${1:-}" == "--baseline" ]]; then
    FAULT_MODE=false
fi

if [[ ! -x "$AUT" ]]; then
    echo "FAIL: AUT binary not found — run 'make -C examples' first"
    exit 1
fi

# ---------------------------------------------------------------------------
# Load the GPIO device and export env vars.
# ---------------------------------------------------------------------------
while IFS= read -r line; do
    case "$line" in VIRTRTLAB_*) export "$line" ;; esac
done < <(virtrtlabctl up gpio0 2>/dev/null)

if [[ -z "${VIRTRTLAB_GPIOCHIP0:-}" ]]; then
    echo "FAIL: virtrtlabctl up gpio0 did not export VIRTRTLAB_GPIOCHIP0"
    exit 1
fi

# ---------------------------------------------------------------------------
# Pre-set line 1 to the initial state before the AUT subscribes to edges.
# This ensures the first injection after the AUT starts produces a genuine
# edge transition.
#
#   Baseline: line starts LOW  (0); we inject rising  (0→1) after AUT starts.
#   Fault:    line starts HIGH (1); we inject falling (1→0) after AUT starts.
# ---------------------------------------------------------------------------
if $FAULT_MODE; then
    virtrtlabctl inject gpio0 1 1  # set line 1 high before AUT subscribes
else
    virtrtlabctl inject gpio0 1 0  # set line 1 low before AUT subscribes
fi

# ---------------------------------------------------------------------------
# Launch the AUT under a hard timeout.
# ---------------------------------------------------------------------------
timeout "$AUT_TIMEOUT" "$AUT" &
AUT_PID=$!

# Allow the AUT time to open the gpiochip and subscribe to edge events.
sleep 0.5

# ---------------------------------------------------------------------------
# Inject the edge transition.
#
#   Baseline: rising  (0→1) — AUT sees it, exits 0.
#   Fault:    falling (1→0) — AUT misses it (wrong polarity), times out, exits 1.
# ---------------------------------------------------------------------------
if $FAULT_MODE; then
    virtrtlabctl inject gpio0 1 0  # falling edge — AUT won't see this
else
    virtrtlabctl inject gpio0 1 1  # rising edge — AUT will see this
fi

# ---------------------------------------------------------------------------
# Wait for the AUT and assert the expected outcome.
# ---------------------------------------------------------------------------
wait "$AUT_PID" 2>/dev/null || true
AUT_RC=$?

if $FAULT_MODE; then
    # AUT must time out (exit 1) because it missed the falling edge.
    if [[ "$AUT_RC" -ne 0 ]]; then
        echo "PASS: AUT timed out as expected — polarity bug triggered (exit $AUT_RC)"
        exit "$AUT_RC"   # non-zero — fault was injected
    else
        echo "FAIL: AUT detected the falling edge despite subscribing to rising only"
        exit 1
    fi
else
    if [[ "$AUT_RC" -eq 0 ]]; then
        echo "PASS: AUT detected the rising edge in baseline mode"
        exit 0
    else
        echo "FAIL: AUT missed the rising edge in baseline mode (exit $AUT_RC)"
        exit "$AUT_RC"
    fi
fi
