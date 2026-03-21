#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
#
# harness.sh — fault-injection harness for aut_gpio_polarity
#
# Scenario
# --------
#   The AUT polls GPIO line 1 every 100 ms to detect a "device ready" signal
#   that is active-HIGH: the device is ready when the line is HIGH (1).
#
#   Bug: the AUT uses active-LOW logic — it checks  if (val == 0)  instead of
#        if (val == 1).
#
#   Baseline: harness pre-sets line 1 LOW (0) before the AUT starts.
#             The buggy check (val == 0) fires immediately: the AUT exits 0,
#             appearing to succeed even though it used the wrong polarity.
#             This hides the bug.
#
#   Fault:    harness pre-sets line 1 HIGH (1) — the real "device ready"
#             level.  The buggy check (val == 0) never fires; the AUT times
#             out (3 s) and exits 1, exposing the polarity bug.
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
while IFS= read -r kv; do
    export "$kv"
done < <(virtrtlabctl up --gpio 1 2>/dev/null | grep -oE 'VIRTRTLAB_[A-Z0-9]+=[^ ]+')

if [[ -z "${VIRTRTLAB_GPIOCHIP0:-}" ]]; then
    echo "FAIL: virtrtlabctl up --gpio 1 did not export VIRTRTLAB_GPIOCHIP0"
    exit 1
fi

# ---------------------------------------------------------------------------
# Pre-set the GPIO line BEFORE the AUT starts so it reads the expected value
# on its very first poll iteration.
#
#   Baseline: line LOW  (0) — the buggy check (val==0) fires → AUT exits 0.
#   Fault:    line HIGH (1) — the correct "ready" level that the buggy AUT
#             fails to detect → timeout → AUT exits 1.
# ---------------------------------------------------------------------------
if $FAULT_MODE; then
    virtrtlabctl inject gpio0 1 1   # HIGH — real "ready" signal
else
    virtrtlabctl inject gpio0 1 0   # LOW  — the level the buggy code detects
fi

# ---------------------------------------------------------------------------
# Run the AUT in the background under a hard timeout.
# ---------------------------------------------------------------------------
timeout "$AUT_TIMEOUT" "$AUT" &
AUT_PID=$!

# ---------------------------------------------------------------------------
# Wait for the AUT and assert the expected outcome.
# Same set -e-safe pattern: "AUT_RC=0; wait PID || AUT_RC=$?"
# ---------------------------------------------------------------------------
AUT_RC=0
wait "$AUT_PID" || AUT_RC=$?

if $FAULT_MODE; then
    # AUT must time out (exit 1) because it missed the HIGH signal.
    if [[ "$AUT_RC" -ne 0 ]]; then
        echo "PASS: AUT timed out as expected — polarity bug triggered (exit $AUT_RC)"
        exit "$AUT_RC"   # non-zero — fault was injected
    else
        echo "FAIL: AUT detected the HIGH signal despite active-LOW bug"
        exit 1
    fi
else
    if [[ "$AUT_RC" -eq 0 ]]; then
        echo "PASS: AUT detected the LOW signal in baseline mode (polarity bug hidden)"
        exit 0
    else
        echo "FAIL: AUT failed in baseline mode (exit $AUT_RC)"
        exit "$AUT_RC"
    fi
fi
