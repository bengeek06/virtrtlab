<!-- SPDX-License-Identifier: CC-BY-4.0 -->

# VirtRTLab

[![CI](https://github.com/bengeek06/virtrtlab/actions/workflows/ci.yml/badge.svg)](https://github.com/bengeek06/virtrtlab/actions/workflows/ci.yml)

**VirtRTLab** is a Linux kernel framework that simulates real-time peripherals — UART, GPIO, and more — so you can run an embedded application under test (AUT) against hardware faults **without physical hardware**.

---

## Why VirtRTLab?

Embedded applications often break under subtle timing conditions: a corrupted byte in a UART frame, a GPIO line that briefly flickers, a bus that goes silent for a few milliseconds. These bugs are reproducible on hardware but nearly impossible to trigger reliably for testing.

VirtRTLab lets you:

- **Simulate peripherals as standard Linux devices** — your AUT opens `/dev/ttyVIRTLAB0` or `/dev/gpiochip4` exactly as it would on real hardware, with no code changes.
- **Inject faults on demand** — latency, jitter, byte drops, bit flips, and GPIO polarity errors via a simple sysfs interface or the `virtrtlabctl` CLI.
- **Run deterministic CI scenarios** — the same fault, the same seed, the same outcome. Every time.
- **Surface race conditions and timing bugs** — faults are applied at kernel level, before the AUT reads the data, with nanosecond-resolution timing.

---

## Architecture overview

```
┌─────────────────────────────────────────────────────┐
│                Application Under Test               │
│  open("/dev/ttyVIRTLAB0")    open("/dev/gpiochip4") │
└────────────────┬────────────────────────┬───────────┘
                 │  standard termios/GPIO  │
        ┌────────▼────────┐      ┌────────▼────────┐
        │ virtrtlab_uart  │      │ virtrtlab_gpio  │
        │  hrtimer pacing │      │  gpiochip API   │
        │  fault engine   │      │  fault engine   │
        └────────┬────────┘      └────────┬────────┘
                 │                        │
        ┌────────▼────────┐      ┌────────▼────────┐
        │ /dev/wire0      │      │ sysfs inject    │
        └────────┬────────┘      └─────────────────┘
                 │
        ┌────────▼────────┐
        │   virtrtlabd    │  ◄── Relay daemon (C, epoll)
        └────────┬────────┘
                 │  AF_UNIX socket
        ┌────────▼────────┐
        │   Simulator     │  ◄── Your test script / socat
        └─────────────────┘

Configuration & fault injection: virtrtlabctl  ──►  sysfs
```

Components:

| Component | Role |
|---|---|
| `virtrtlab_core` | Virtual bus, kobject tree, sysfs root |
| `virtrtlab_uart` | TTY driver + hrtimer pacing + fault injection engine |
| `virtrtlab_gpio` | GPIO chip driver (gpiochip API) + fault injection |
| `virtrtlabd` | Relay daemon: AF_UNIX socket ↔ wire device, one per UART |
| `virtrtlabctl` | CLI: lab lifecycle, sysfs get/set, fault injection, stats |

---

## Installation

### Packaging (.deb)

VirtRTLab now exposes two local package flows:

- `make deb` (default) or `make deb-release-local`: builds the release-style package
     with modules (DKMS source), daemon, CLI, udev rules, and systemd unit.
- `make deb-dkms`: builds the DKMS-oriented developer package path.

Both commands write generated `.deb` files under `dist/`.

### Prerequisites

| Requirement | Debian/Ubuntu package |
|---|---|
| Linux ≥ 5.15 | — |
| Kernel build headers | `linux-headers-$(uname -r)` |
| C compiler | `build-essential` |
| Python ≥ 3.11 | `python3` |
| GPIO library headers | `linux-libc-dev` |
| (optional) toml config | `python3-tomllib` (stdlib since 3.11) |

```sh
sudo apt install build-essential linux-headers-$(uname -r) linux-libc-dev
```

### Building the kernel modules

```sh
git clone https://github.com/bengeek06/virtrtlab.git
cd virtrtlab
make -C kernel/
```

The `Makefile` builds against the running kernel automatically. The output is:

```
kernel/virtrtlab_core.ko
kernel/virtrtlab_uart.ko
kernel/virtrtlab_gpio.ko
```

### Kernel configuration requirements

VirtRTLab modules are **out-of-tree** and require no kernel recompilation for
most features. The following `CONFIG_` options are needed:

| Config | Type | Required by | Present in stock Debian/Ubuntu |
|---|---|---|---|
| `CONFIG_TTY` | bool | virtrtlab_uart | ✅ yes |
| `CONFIG_GPIOLIB` | bool | virtrtlab_gpio | ✅ yes |
| `CONFIG_GPIO_CDEV` | bool | virtrtlab_gpio (chardev API) | ✅ yes |
| `CONFIG_IRQ_SIM` | **bool** | virtrtlab_gpio (GPIO edge events, **planned v0.2.0**) | ⚠️ often missing |

`CONFIG_IRQ_SIM` is **not required for v0.1.0**: the current driver implements
GPIO line state injection but not yet the irqchip edge-event path. It is listed
here as a forward reference so you can prepare your kernel in advance.
See [docs/kernel-requirements.md](docs/kernel-requirements.md) for details.

#### Recompiling the kernel on Debian/Ubuntu (advanced)

Only needed if your kernel has these options disabled (e.g. a minimal embedded build).

**1 — Install build dependencies**

```sh
sudo apt install git fakeroot build-essential ncurses-dev xz-utils libssl-dev \
                 bc flex libelf-dev bison dwarves zstd debhelper
```

**2 — Get the kernel source**

```sh
# Use the same version as your running kernel
KVER=$(uname -r | cut -d- -f1)
wget https://cdn.kernel.org/pub/linux/kernel/v${KVER%%.*}.x/linux-${KVER}.tar.xz
tar xf linux-${KVER}.tar.xz
cd linux-${KVER}
```

**3 — Start from your current config**

```sh
cp /boot/config-$(uname -r) .config
make olddefconfig
```

**4 — Enable required options**

```sh
scripts/config --enable CONFIG_TTY
scripts/config --enable CONFIG_GPIOLIB
scripts/config --enable CONFIG_GPIO_CDEV
# Optional: enable now for v0.2.0 GPIO edge-event support (not required in v0.1.0)
scripts/config --enable CONFIG_IRQ_SIM
make olddefconfig
```

`CONFIG_IRQ_SIM` is a `bool` that must be built into `vmlinux` (not a loadable
module). It will be required by `virtrtlab_gpio` when GPIO edge-event notifications
via the irqchip layer are implemented (planned for v0.2.0). Safe to enable now.

**5 — Build and install Debian packages**

```sh
make -j$(nproc) bindeb-pkg LOCALVERSION=-virtrtlab
sudo dpkg -i ../linux-image-*.deb ../linux-headers-*.deb
sudo reboot
```

**6 — After reboot, build VirtRTLab modules against the new kernel**

```sh
cd /path/to/virtrtlab
make -C kernel/
```

### Building the daemon

```sh
make -C daemon/
# produces daemon/virtrtlabd
```

### System setup

**Create the `virtrtlab` group** (required for non-root access to sockets and GPIO):

```sh
sudo groupadd -r virtrtlab
sudo usermod -aG virtrtlab $USER
# Log out and back in, or: newgrp virtrtlab
```

**Install udev rules** (permissions for `/dev/gpiochipN` and sysfs `inject`):

```sh
sudo cp install/90-virtrtlab.rules /lib/udev/rules.d/
sudo udevadm control --reload-rules
```

**Install the CLI** (optional — can be run from the repo):

```sh
sudo install -m 755 cli/virtrtlabctl.py /usr/local/bin/virtrtlabctl
sudo install -m 755 daemon/virtrtlabd /usr/local/sbin/virtrtlabd
```

---

## Getting Started

### 1 — Bring the lab up

```sh
sudo virtrtlabctl up --uart 1 --gpio 1
```

This loads `virtrtlab_core`, `virtrtlab_uart`, `virtrtlab_gpio`, and starts `virtrtlabd`. Output:

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

Use `--json` to get machine-readable output:

```sh
sudo virtrtlabctl --json up --uart 1 --gpio 1
```

### 2 — Verify the lab

```sh
virtrtlabctl status
virtrtlabctl list devices
```

### 3 — Inject a fault

```sh
# Inject 5% byte drop on UART transmit path
virtrtlabctl set uart0 drop_rate_ppm=50000

# Inject 500 µs latency
virtrtlabctl set uart0 latency_ns=500000

# Force GPIO line 0 to physical HIGH
virtrtlabctl inject gpio0 0 1
```

### 4 — Read stats

```sh
virtrtlabctl stats uart0
virtrtlabctl stats gpio0
```

### 5 — Tear down

```sh
sudo virtrtlabctl down
```

---

## Examples

The `examples/` directory contains three self-contained fault-injection scenarios. Each has an **AUT** (`aut.c`) with a deliberate bug, and a **harness** (`harness.sh`) that injects the fault and asserts the observable failure.

Build all AUTs first:

```sh
make -C examples/
```

| Example | Bug | Fault | Observable failure |
|---|---|---|---|
| `aut_uart_timeout` | `read()` with no timeout (`VMIN=1, VTIME=0`) | Last byte of 4-byte frame dropped | AUT blocks forever |
| `aut_uart_statemachine` | State machine has no RESET state | Extra byte injected before checksum | AUT exits 2 (bad state) |
| `aut_gpio_polarity` | Active-LOW check on active-HIGH signal | Line pre-set HIGH before AUT starts | AUT times out (exit 1) |

### Running an example

**Baseline** — no fault, AUT should pass:

```sh
examples/aut_uart_timeout/harness.sh --baseline
# exit 0  — AUT completed successfully
```

**Fault injection** — bug is triggered:

```sh
examples/aut_uart_timeout/harness.sh
# exit 124  — AUT hung (timeout killed it), fault confirmed
```

### How the harnesses work

Each `harness.sh`:

1. Calls `virtrtlabctl up` to load modules and start the daemon  
2. Launches the AUT in the background with `timeout`  
3. Injects the fault via `virtrtlabctl set` or `virtrtlabctl inject`  
4. Sends the stimulus (bytes via `socat`, or GPIO line state)  
5. Asserts the expected outcome (exit code, timing)  
6. Calls `virtrtlabctl down` in cleanup

See `examples/README.md` for full details on each scenario.

---

## Documentation

| Document | Contents |
|---|---|
| [docs/virtrtlabctl.md](docs/virtrtlabctl.md) | Complete CLI reference — all commands, options, output formats |
| [docs/daemon.md](docs/daemon.md) | `virtrtlabd` user guide — configuration, signals, reconnect, logging |
| [docs/sysfs.md](docs/sysfs.md) | sysfs API reference — all attributes, types, error conditions |
| [docs/socket-api.md](docs/socket-api.md) | Wire device and socket transport specification |
| [docs/spec.md](docs/spec.md) | Architecture and interface specification (naming conventions, data path, design decisions) |

---

## Project status

| Milestone | Status | Peripherals |
|---|---|---|
| v0.1.0 | In progress | UART, GPIO |
| v0.2.0 | Planned | CAN, named profiles, record/replay |
| v0.3.0 | Planned | Tracepoints, full CI integration |

---

## License

Source code: [MIT](LICENSE.MIT)  
Documentation: [CC-BY-4.0](LICENSE.CC-BY-4.0)
