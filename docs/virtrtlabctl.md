<!-- SPDX-License-Identifier: CC-BY-4.0 -->

# `virtrtlabctl` — CLI Reference

`virtrtlabctl` is the command-line interface for VirtRTLab. It manages the lab lifecycle (loading modules, starting the daemon), reads and writes sysfs attributes, injects faults, and emits the AUT integration contract.

---

## Synopsis

```
virtrtlabctl [--json] [--no-sudo] <command> [options]
```

### Global flags

| Flag | Description |
|---|---|
| `--json` | Emit all output as JSON (machine-readable). Errors are also JSON: `{"error": "...", "code": N}` |
| `--no-sudo` | Do not prepend `sudo` to privileged operations. Use when already running as root or with capabilities. |

---

## Commands

### `up` — Bring up a lab

Loads kernel modules, starts the daemon, and emits the AUT integration contract.

```
virtrtlabctl up [--config FILE] [--uart N] [--gpio N]
```

| Option | Description |
|---|---|
| `--config FILE` | Path to a TOML lab profile. Overrides `--uart` and `--gpio`. |
| `--uart N` | Number of UART instances to load (default: 1) |
| `--gpio N` | Number of GPIO instances to load (default: 0) |

**What it does:**

1. Locates `virtrtlab_core.ko`, `virtrtlab_uart.ko`, `virtrtlab_gpio.ko` (searches `MODULE_DIR`, current directory, `modinfo`, installed modules).
2. Calls `insmod` for each required module.
3. Starts `virtrtlabd` for UART instances.
4. Waits up to 5 s for all daemon sockets to appear.
5. Emits the AUT integration contract on stdout.

**Output (human-readable, default):**

One block per device showing the resolved paths and `export KEY=VALUE` lines:

```
[ok] uart0 loaded
     tty: /dev/ttyVIRTLAB0
     export VIRTRTLAB_UART0=/dev/ttyVIRTLAB0

[ok] gpio0 loaded
     gpiochip: /dev/gpiochip4
     sysfs base: 496
     control: /sys/kernel/virtrtlab/devices/gpio0
     export VIRTRTLAB_GPIOCHIP0=/dev/gpiochip4
     export VIRTRTLAB_GPIOBASE0=496
     export VIRTRTLAB_GPIOCTRL0=/sys/kernel/virtrtlab/devices/gpio0
```

> `VIRTRTLAB_GPIOBASE<N>` is omitted with a warning if the legacy sysfs GPIO ABI
> (`CONFIG_GPIO_SYSFS`) is not available on the host kernel.

**Output (JSON, with `--json`):**

```json
{
  "devices": [
    {
      "name": "uart0",
      "type": "uart",
      "aut_path": "/dev/ttyVIRTLAB0",
      "wire_path": "/dev/virtrtlab-wire0",
      "socket_path": "/run/virtrtlab/uart0.sock",
      "env": {"VIRTRTLAB_UART0": "/dev/ttyVIRTLAB0"}
    },
    {
      "name": "gpio0",
      "type": "gpio",
      "chip_path": "/dev/gpiochip4",
      "control_path": "/sys/kernel/virtrtlab/devices/gpio0",
      "sysfs_base": 496,
      "env": {
        "VIRTRTLAB_GPIOCHIP0": "/dev/gpiochip4",
        "VIRTRTLAB_GPIOBASE0": "496",
        "VIRTRTLAB_GPIOCTRL0": "/sys/kernel/virtrtlab/devices/gpio0"
      }
    }
  ]
}
```

**Exit codes:**

| Code | Meaning |
|---|---|
| 0 | Lab is up |
| 1 | Module load or daemon start failed |

---

### `down` — Tear down the lab

Stops the daemon and unloads modules.

```
virtrtlabctl down
```

**What it does:**

1. Sends SIGTERM to `virtrtlabd` and waits for it to exit cleanly.
2. Calls `rmmod` for `virtrtlab_gpio`, `virtrtlab_uart`, `virtrtlab_core` in dependency order.

**Exit codes:**

| Code | Meaning |
|---|---|
| 0 | Command completed; module unloads are best-effort and do not affect the exit code |

> If some modules fail to unload (e.g. still in use by a process), they are
> silently skipped. Check `virtrtlabctl status` or `lsmod` to verify.

---

### `status` — Global lab status

```
virtrtlabctl status
```

Prints the status of modules, daemon, sockets, and the virtual bus.

**Output (default):**

```
modules:
  virtrtlab_core            loaded
  virtrtlab_uart            loaded
  virtrtlab_gpio            loaded

daemon:
  pid    12345
  state  running

sockets:
  /run/virtrtlab/uart0.sock

bus vrtlbus0:
  state  up
```

**Output (JSON):**

```json
{
  "modules": {
    "virtrtlab_core": "loaded",
    "virtrtlab_uart": "loaded",
    "virtrtlab_gpio": "loaded"
  },
  "daemon": {"state": "running", "pid": 12345},
  "sockets": ["/run/virtrtlab/uart0.sock"],
  "bus": {"vrtlbus0": {"state": "up"}}
}
```

---

### `list` — Discover buses and devices

```
virtrtlabctl list buses
virtrtlabctl list devices [--type TYPE]
```

**`list buses`** — lists all virtual buses registered in sysfs.

**`list devices`** — lists all registered devices. Use `--type uart` or `--type gpio` to filter.

**Output example (`list devices`):**

```
uart0                type=uart     bus=vrtlbus0     enabled=yes
gpio0                type=gpio     bus=vrtlbus0     enabled=yes
```

**JSON output (`list devices`):**

```json
{"devices": [
  {"name": "uart0", "type": "uart", "bus": "vrtlbus0", "enabled": true},
  {"name": "gpio0", "type": "gpio", "bus": "vrtlbus0", "enabled": true}
]}
```

**JSON output (`list buses`):**

```json
{"buses": ["vrtlbus0"]}
```

---

### `get` — Read a sysfs attribute

```
virtrtlabctl get <target> <attr>
```

| Argument | Description |
|---|---|
| `target` | Device name (`uart0`, `gpio0`) or bus name (`vrtlbus0`) |
| `attr` | Attribute name (e.g. `baud`, `latency_ns`, `chip_path`) |

**Examples:**

```sh
virtrtlabctl get uart0 baud
# 115200

virtrtlabctl get uart0 latency_ns
# 0

virtrtlabctl get gpio0 chip_path
# /dev/gpiochip4

virtrtlabctl get vrtlbus0 state
# up
```

**JSON output:**

```sh
virtrtlabctl --json get uart0 baud
# {"target": "uart0", "attr": "baud", "value": "115200"}
```

**Exit codes:**

| Code | Meaning |
|---|---|
| 0 | Success |
| 4 | Target or attribute not found |

---

### `set` — Write sysfs attribute(s)

```
virtrtlabctl set <target> <attr=value> [<attr=value> …]
```

Multiple assignments are applied in order. The command fails fast on the first error.

**Examples:**

```sh
# Arm a 500 µs latency fault
virtrtlabctl set uart0 latency_ns=500000

# Add 10% jitter on top
virtrtlabctl set uart0 jitter_ns=50000

# Drop 5% of bytes (50,000 PPM)
virtrtlabctl set uart0 drop_rate_ppm=50000

# Flip bits in 1% of bytes
virtrtlabctl set uart0 bitflip_rate_ppm=10000

# Multiple assignments in one call
virtrtlabctl set uart0 latency_ns=0 jitter_ns=0 drop_rate_ppm=0

# Disable a device
virtrtlabctl set uart0 enabled=0

# Halt the bus
virtrtlabctl set vrtlbus0 state=down

# Reset the bus (clears faults, re-enables all devices, resets stats)
virtrtlabctl set vrtlbus0 state=reset
```

**Exit codes:**

| Code | Meaning |
|---|---|
| 0 | All assignments applied |
| 2 | Invalid `attr=value` syntax (missing `=`, empty attribute name) |
| 4 | Attribute not found, or kernel rejected the write (`-EINVAL`, `-EBUSY`, …) |

---

### `stats` — Display per-device counters

```
virtrtlabctl stats <device>
```

**Examples:**

```sh
virtrtlabctl stats uart0
```

Output:

```
uart0 stats:
  tx_bytes             102400
  rx_bytes              98304
  overruns                  0
  drops                   512
```

```sh
virtrtlabctl stats gpio0
```

Output:

```
gpio0 stats:
  bitflips                  3
  drops                     0
  value_changes            47
```

**JSON output:**

```sh
virtrtlabctl --json stats uart0
# {"device": "uart0", "stats": {"tx_bytes": 102400, "rx_bytes": 98304, "overruns": 0, "drops": 512}}
```

---

### `reset` — Reset stats counters

```
virtrtlabctl reset <device>
```

Resets all stats counters for the device to zero (writes `0` to `stats/reset`). Does **not** clear fault attributes or device `enabled` state.

```sh
virtrtlabctl reset uart0
virtrtlabctl reset gpio0
```

To reset everything (fault attrs, enabled state, stats), use the bus reset:

```sh
virtrtlabctl set vrtlbus0 state=reset
```

---

### `inject` — Inject a GPIO line value

```
virtrtlabctl inject <device> <line> <value>
```

| Argument | Description |
|---|---|
| `device` | GPIO device name, e.g. `gpio0` |
| `line` | GPIO line index, 0–7 |
| `value` | Physical value: `0` (LOW) or `1` (HIGH) |

This writes `<line>:<value>` to `/sys/kernel/virtrtlab/devices/<device>/inject`. Active fault attributes (`latency_ns`, `jitter_ns`, `drop_rate_ppm`, `bitflip_rate_ppm`) are applied to the transition.

**Examples:**

```sh
# Set line 0 HIGH
virtrtlabctl inject gpio0 0 1

# Set line 3 LOW
virtrtlabctl inject gpio0 3 0

# Set line 1 HIGH with an active latency fault
virtrtlabctl set gpio0 latency_ns=200000
virtrtlabctl inject gpio0 1 1
```

**Exit codes:**

| Code | Meaning |
|---|---|
| 0 | Injection accepted |
| 2 | Invalid line or value (bad argument) |
| 4 | Device not found, inject not supported, or kernel rejected |

---

### `daemon` — Manage `virtrtlabd` independently

Manages the daemon lifecycle without touching kernel modules. Useful when the modules are already loaded and you only want to restart the relay daemon.

```
virtrtlabctl daemon start [--num-uarts N] [--run-dir DIR]
virtrtlabctl daemon stop
virtrtlabctl daemon status
```

| Subcommand | Description |
|---|---|
| `start` | Start the daemon. Fails with exit 3 if already running. |
| `stop` | Send SIGTERM and wait for clean exit. |
| `status` | Print daemon state and PID. Exit 0 if running, 3 if stopped. |

**Options for `daemon start`:**

| Option | Default | Description |
|---|---|---|
| `--num-uarts N` | `1` | Number of UART sockets to create |
| `--run-dir DIR` | `/run/virtrtlab` | Directory for PID file and sockets |

**Examples:**

```sh
# Start daemon for 2 UART instances
sudo virtrtlabctl daemon start --num-uarts 2

# Check status
virtrtlabctl daemon status
# state  running
# pid    9876

# Stop gracefully
sudo virtrtlabctl daemon stop
```

**Exit codes for `daemon status`:**

| Code | Meaning |
|---|---|
| 0 | Daemon is running |
| 3 | Daemon is stopped |

---

## Lab profiles (TOML)

A TOML profile lets you version-control your lab configuration.
Devices are declared as an array of tables under `[[devices]]`.

```toml
# lab.toml

[[devices]]
type = "uart"
count = 2

[[devices]]
type = "gpio"
count = 1

[build]
# module_dir = "/path/to/built/modules"  # optional; overrides module search path

[bus]
# seed = 42  # optional PRNG seed written to vrtlbus0/seed at startup
```

Usage:

```sh
sudo virtrtlabctl up --config lab.toml
```

Inline flags (`--uart N`, `--gpio N`) override the corresponding `[[devices]]` entries
from the profile. A profile is not required: if neither `--config` nor inline flags are
given, `virtrtlabctl` searches for `./lab.toml` and `/etc/virtrtlab/lab.toml`.

---

## Complete fault injection example

```sh
# 1. Bring up the lab (human-readable output; set env vars from the export lines)
sudo virtrtlabctl up --uart 1 --gpio 1
export VIRTRTLAB_UART0=/dev/ttyVIRTLAB0

# 2. Confirm lab state
virtrtlabctl status
virtrtlabctl list devices

# 3. Arm a UART fault: 2% drop + 100 µs latency
virtrtlabctl set uart0 drop_rate_ppm=20000 latency_ns=100000

# 4. Connect a simulator
socat - UNIX-CONNECT:/run/virtrtlab/uart0.sock &

# 5. Run your AUT (10 s timeout)
timeout 10 "$AUT_BINARY" "$VIRTRTLAB_UART0"
echo "AUT exit: $?"

# 6. Check stats
virtrtlabctl stats uart0

# 7. Clear faults, reset stats
virtrtlabctl set uart0 drop_rate_ppm=0 latency_ns=0
virtrtlabctl reset uart0

# 8. Tear down
sudo virtrtlabctl down
```

---

## Exit code summary

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Operational error (module load failure, daemon error, filesystem error) |
| 2 | Bad argument or profile error (invalid syntax, unknown device type) |
| 3 | Timeout waiting for sockets; `daemon status` → stopped; `daemon start` → already running |
| 4 | Not found or kernel rejected (device, attribute, inject path) |

---

## See also

- [daemon.md](daemon.md) — `virtrtlabd` user guide (direct daemon usage, logging, signals)
- [sysfs.md](sysfs.md) — full sysfs attribute reference
- [spec.md](spec.md) — architecture and AUT integration contract details
