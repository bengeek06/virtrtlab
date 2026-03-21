<!-- SPDX-License-Identifier: CC-BY-4.0 -->

# VirtRTLab — Getting Started with Fault Injection

This directory contains three self-contained examples that demonstrate the
VirtRTLab fault-injection workflow end to end.  Each example has:

- An **AUT** (`aut.c`) — a small C program that communicates with a
  simulated peripheral.  The AUT is correct under normal conditions but
  contains a subtle bug that only surfaces under a specific fault.
- A **harness** (`harness.sh`) — a shell script that brings up the device
  via `virtrtlabctl`, starts the AUT, injects the fault, and asserts the
  observed failure.

| Example | Peripheral | Bug | Fault injected | Observable failure |
|---|---|---|---|---|
| `aut_uart_timeout` | UART | `read()` with VMIN=4, VTIME=0 (no timeout) | Harness drops the last byte of the 4-byte frame | AUT blocks forever |
| `aut_gpio_polarity` | GPIO | Subscribes to RISING edge; signal is FALLING | Harness drives 1→0 instead of 0→1 | AUT times out, misses the event |
| `aut_uart_statemachine` | UART | State machine has no RESET state | Harness injects an extra byte before the checksum | AUT exits 2 (bad state) |

---

## Prerequisites

- Linux kernel ≥ 5.10 with `CONFIG_GPIOLIB`, `CONFIG_GPIO_CDEV` enabled
- `virtrtlab_core`, `virtrtlab_uart`, and `virtrtlab_gpio` modules loaded
- `virtrtlabd` daemon running (for UART examples)
- `virtrtlabctl` in `PATH`
- `gcc`, `make`, `python3` installed on the host

---

## Quick Start

### 1 — Build the AUTs

```sh
make -C examples
```

All three AUT binaries are built against the host libc; no kernel source is
required (except `linux-libc-dev` for `<linux/gpio.h>` in `aut_gpio_polarity`).

### 2 — Load the kernel modules

```sh
sudo modprobe virtrtlab_core
sudo modprobe virtrtlab_uart
sudo modprobe virtrtlab_gpio
```

### 3 — Start the daemon (UART examples only)

```sh
sudo virtrtlabd &
```

### 4 — Run an example

**Baseline** (no fault — AUT passes):

```sh
examples/aut_uart_timeout/harness.sh --baseline
# PASS: AUT completed successfully in baseline mode
# exit 0
```

**Fault injection** (bug is triggered):

```sh
examples/aut_uart_timeout/harness.sh
# PASS: AUT hung as expected under fault injection (exit 124)
# exit 124  (non-zero — fault was injected)
```

---

## Example Details

### `aut_uart_timeout` — missing `read()` timeout

**Bug location**: `aut.c`, termios configuration.

```c
tio.c_cc[VMIN]  = 4;
tio.c_cc[VTIME] = 0;   /* BUG: no inter-character timeout */
```

`VTIME=0` means `read()` blocks indefinitely until `VMIN` bytes arrive.  If
the simulator drops the last byte, the AUT hangs forever.

**Fix**: set `VTIME` to a finite value (e.g. `VTIME=30` for 3 s), or use
`poll()`/`select()` with a timeout before calling `read()`.

See also: [`docs/socket-api.md`](../docs/socket-api.md) — daemon socket protocol,
relay behaviour.

---

### `aut_gpio_polarity` — wrong edge polarity

**Bug location**: `aut.c`, GPIO v2 line request.

```c
req.config.flags = GPIO_V2_LINE_FLAG_INPUT |
                   GPIO_V2_LINE_FLAG_EDGE_RISING;  /* BUG: signal is FALLING */
```

The physical signal of interest goes from 1 → 0 (falling).  The AUT registers
only for rising edges and therefore never receives the event.

**Fix**: subscribe to `GPIO_V2_LINE_FLAG_EDGE_FALLING` (or both edges) and
filter on `event.id`.

See also: [`docs/sysfs.md` — GPIO inject attr](../docs/sysfs.md) — how
`virtrtlabctl inject` drives input transitions through the fault-injection shim.

---

### `aut_uart_statemachine` — no RESET state

**Bug location**: `aut.c`, `GOT_DATA` case of the state machine.

```c
case GOT_DATA: {
    unsigned char expected = cmd ^ data;
    if (b != expected) {
        /* BUG: no RESET — exits immediately, cannot recover */
        return 2;
    }
    ...
}
```

If an extra byte arrives between the data byte and the checksum byte, the state
machine consumes the injected byte as the checksum, fails the XOR check, and
exits 2 with no way to resynchronise.

**Fix**: on mismatch in `GOT_DATA`, transition back to `IDLE` (look for the
next SOF byte) instead of exiting, or adopt a length-prefixed framing protocol.

See also: [`docs/socket-api.md`](../docs/socket-api.md) — relay behaviour,
simulator ↔ AUT byte stream semantics.

---

## Harness exit codes

| Mode | AUT outcome | Harness exit |
|---|---|---|
| `--baseline` | AUT exits 0 | `0` (PASS) |
| `--baseline` | AUT fails | Non-zero (FAIL — unexpected) |
| fault (default) | AUT fails as expected | Non-zero (PASS — fault triggered) |
| fault (default) | AUT exits 0 despite fault | `1` (FAIL — bug not triggered) |

The non-zero exit in fault mode allows `pytest` to capture the harness output
and assert `returncode != 0` as the expected outcome:

```python
result = subprocess.run(["examples/aut_uart_timeout/harness.sh"], capture_output=True)
assert result.returncode != 0            # fault mode: AUT should fail
assert "PASS" in result.stdout.decode()  # harness asserts the failure

result = subprocess.run(["examples/aut_uart_timeout/harness.sh", "--baseline"], ...)
assert result.returncode == 0            # baseline: AUT should succeed
```
