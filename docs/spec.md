<!-- SPDX-License-Identifier: CC-BY-4.0 -->

# VirtRTLab — Architecture and Interface Specification

This document is the source of truth for naming conventions, data paths, module parameters, and design decisions. It is aimed at contributors and integrators who need to understand or extend VirtRTLab internals.

For user-facing documentation, see:
- [README.md](../README.md) — installation and getting started
- [virtrtlabctl.md](virtrtlabctl.md) — CLI reference
- [sysfs.md](sysfs.md) — sysfs attribute reference
- [socket-api.md](socket-api.md) — wire device and socket transport

---

## 1. Naming conventions

### Project and prefixes

| Surface | Convention |
|---|---|
| Project name | **VirtRTLab** |
| Kernel symbols, Kconfig | `VIRTRTLAB_…` / `virtrtlab_…` |
| sysfs namespace | `virtrtlab` |
| CLI binary | `virtrtlabctl` |
| Daemon binary | `virtrtlabd` |
| State directory | `/run/virtrtlab/` |
| Sockets | `/run/virtrtlab/uart0.sock`, `uart1.sock`, … |

### Module names

| Module | Role |
|---|---|
| `virtrtlab_core` | Virtual bus + kobject tree + common infra |
| `virtrtlab_uart` | UART peripheral (TTY driver, hrtimer, fault engine) |
| `virtrtlab_gpio` | GPIO peripheral (gpiochip, inject, fault engine) |
| `virtrtlab_spi` | *(planned v0.2.0)* |
| `virtrtlab_can` | *(planned v0.2.0)* |
| `virtrtlab_adc` | *(planned v0.2.0)* |
| `virtrtlab_dac` | *(planned v0.2.0)* |

### Object naming

| Object | Name | Example |
|---|---|---|
| Bus instance | `vrtlbus<N>` | `vrtlbus0` |
| Device instance | `<type><N>` | `uart0`, `gpio0` |
| AUT TTY node | `/dev/ttyVIRTLAB<N>` | `/dev/ttyVIRTLAB0` |
| Daemon wire device | `/dev/virtrtlab-wire<N>` | `/dev/virtrtlab-wire0` |
| GPIO chardev | `/dev/gpiochip<M>` | `/dev/gpiochip4` (M assigned by gpiolib) |
| Daemon socket | `/run/virtrtlab/<type><N>.sock` | `/run/virtrtlab/uart0.sock` |
| sysfs control root | `/sys/kernel/virtrtlab/devices/<dev>/` | `.../devices/uart0/` |

### Module parameters

**`virtrtlab_uart`**

- `num_uart_devices` (int, default `1`, range `1..4`) — number of UART instances at load time.  
  With `num_uart_devices=2`: `uart0`, `uart1`, `/dev/ttyVIRTLAB0`, `/dev/ttyVIRTLAB1`, `/dev/virtrtlab-wire0`, `/dev/virtrtlab-wire1`.

**`virtrtlab_gpio`**

- `num_gpio_devs` (int, default `1`, range `1..32`) — number of GPIO bank instances at load time.  
  Each `gpioN` models **one 8-line bank**. With `num_gpio_devs=2`: `gpio0`, `gpio1`.

---

## 2. Architecture

### Data path (UART)

```
AUT ──[termios/read()/write()]──► /dev/ttyVIRTLABx
                                          │
                                  virtrtlab_uart
                              (hrtimer TX pacing + fault engine)
                                          │
                                  /dev/virtrtlab-wireN
                                          │
                                    virtrtlabd
                                  (epoll relay loop)
                                          │
                              /run/virtrtlab/uartN.sock
                                          │
                                      Simulator
```

Fault injection (latency, jitter, drop, bitflip) is applied in the kernel **before** bytes reach the wire device. The wire device and daemon are transparent to faults.

### Data path (GPIO)

```
Harness ──[sysfs write]──► /sys/kernel/virtrtlab/devices/gpio0/inject
                                         │
                                 virtrtlab_gpio
                             (fault engine: latency, jitter, drop, bitflip)
                                         │
                                 gpiochip line state
                                         │
                         AUT ──[gpio_get_value()/GPIO v2 ioctl]──►
```

The AUT reads the injected value via the standard GPIO chardev API (`/dev/gpiochipN`, ioctl `GPIO_V2_LINE_GET_VALUES_IOCTL`) or the legacy sysfs ABI.

### Control path

All configuration and fault injection goes through **sysfs**. `virtrtlabctl` is a thin wrapper.

```
virtrtlabctl set uart0 latency_ns=500000
    └──► echo 500000 > /sys/kernel/virtrtlab/devices/uart0/latency_ns

virtrtlabctl inject gpio0 0 1
    └──► echo 0:1 > /sys/kernel/virtrtlab/devices/gpio0/inject
```

### Components table

| Component | Languages | Role |
|---|---|---|
| `virtrtlab_core` | C | Virtual bus (`vrtlbus<N>`), kobject tree, `version` attr |
| `virtrtlab_uart` | C | TTY driver + misc wire device + hrtimer pacing + fault engine |
| `virtrtlab_gpio` | C | GPIO chip driver + fault engine + inject attr |
| `virtrtlabd` | C (GNU11) | Daemon: socket creation, epoll relay loop between wire device and AF_UNIX socket |
| `virtrtlabctl` | Python 3 | CLI: sysfs get/set, module lifecycle, daemon lifecycle, AUT integration contract |

### Why C for the daemon?

The daemon is in the data path for every UART instance simultaneously. CPython's garbage collector introduces multi-millisecond pauses — exactly the kind of jitter VirtRTLab is designed to *measure* in the AUT, not inject at the infrastructure level.

- Static buffers: RSS < 512 KB for 8 instances
- No heap allocation in the relay hot path
- Startup latency: < 5 ms (vs. 50–200 ms for CPython)
- Dependency: libc only

### Repository layout

```
.
├── docs/
│   ├── README.md          — documentation index
│   ├── spec.md            — this file: architecture and interface spec
│   ├── virtrtlabctl.md    — CLI reference
│   ├── daemon.md          — daemon user guide
│   ├── sysfs.md           — sysfs attribute reference
│   └── socket-api.md      — wire device and socket transport spec
├── daemon/
│   ├── Makefile
│   ├── main.c
│   ├── epoll_loop.c
│   ├── epoll_loop.h
│   ├── instance.c
│   └── instance.h
├── kernel/
│   ├── include/
│   ├── virtrtlab_core.c
│   ├── virtrtlab_uart.c
│   └── virtrtlab_gpio.c
├── cli/
│   └── virtrtlabctl.py
├── install/
│   └── 90-virtrtlab.rules
├── examples/
│   ├── aut_uart_timeout/
│   ├── aut_uart_statemachine/
│   └── aut_gpio_polarity/
└── tests/
    ├── kernel/
    ├── daemon/
    ├── cli/
    └── install/
```

---

## 3. sysfs layout

Full attribute reference: [sysfs.md](sysfs.md).

### Root

`/sys/kernel/virtrtlab/`

- `version` (ro): semantic version, e.g. `0.1.0`
- `buses/`
- `devices/`

### Bus instances

`/sys/kernel/virtrtlab/buses/vrtlbus0/`

- `state` (rw): `up|down|reset`
- `clock_ns` (ro): `CLOCK_MONOTONIC` snapshot in ns
- `seed` (rw): xorshift32 PRNG seed for stochastic fault profiles

### Common device attributes

`/sys/kernel/virtrtlab/devices/<dev>/`

| Attribute | Access | Description |
|---|---|---|
| `type` | ro | `uart\|gpio\|spi\|…` |
| `bus` | ro | Parent bus, e.g. `vrtlbus0` |
| `enabled` | rw | Device gate: `0\|1` |
| `latency_ns` | rw | Base delivery latency per transfer unit (ns) |
| `jitter_ns` | rw | Uniform jitter amplitude (ns) |
| `drop_rate_ppm` | rw | Drops per million transfer units |
| `bitflip_rate_ppm` | rw | Payload corruptions per million transfer units |

---

## 4. Wire device and daemon socket

Full transport specification: [socket-api.md](socket-api.md).

### Wire device (`/dev/virtrtlab-wireN`)

- Raw byte pipe between `virtrtlab_uart` (kernel) and `virtrtlabd` (daemon)
- Supports `read()`, `write()`, `poll()`/`select()`
- Opened exclusively by `virtrtlabd` — `open()` returns `-EBUSY` if already open
- Fault injection is applied **before** bytes reach the wire device

### Daemon socket (`/run/virtrtlab/uartN.sock`)

- `AF_UNIX`, `SOCK_STREAM`, raw bytes
- One per UART instance
- Created by daemon at startup, removed on clean shutdown
- No framing, no length prefix — raw byte stream
- Single active connection; a second `connect()` is rejected

---

## 5. AUT integration contract

`virtrtlabctl up` emits a stable set of environment variables for the AUT to consume:

| Variable | Example value | Description |
|---|---|---|
| `VIRTRTLAB_UART_TTY` | `ttyVIRTLAB0` | TTY device name (without `/dev/`) |
| `VIRTRTLAB_UART_SOCK` | `/run/virtrtlab/uart0.sock` | Daemon socket path |
| `VIRTRTLAB_GPIO_CHIP` | `/dev/gpiochip4` | GPIO chardev path |
| `VIRTRTLAB_GPIO_SYSFS_BASE` | `496` | First global GPIO number (legacy ABI) |

Usage in a harness:

```sh
eval $(sudo virtrtlabctl up --uart 1 --gpio 1)
# AUT can now use $VIRTRTLAB_UART_TTY, $VIRTRTLAB_GPIO_CHIP, …
```

---

## 6. Fault injection model

### Direction (v0.1.0)

Fault attributes apply to the **AUT-driven transmit path** for UART and to the **sysfs-injected input path** for GPIO. Simulator→AUT UART traffic and AUT-driven GPIO outputs are **not** mutated in v0.1.0.

### Transfer unit

| Peripheral | Transfer unit |
|---|---|
| UART | One byte paced by the hrtimer |
| GPIO | One sysfs write to `inject` (one line transition) |

### Timing

- `latency_ns` and `jitter_ns` take effect from the **next transfer scheduling point** after the sysfs store returns.
- For GPIO: the kernel snapshots the line index, value, and fault attrs at the moment the `inject` write is accepted. A later change to fault attrs does not affect an already-scheduled transition.

### Fault gate evaluation per transfer unit

```
1. Drop gate:   draw PRNG → drop_rate_ppm / 1_000_000 probability → stats/drops++
2. Bitflip gate: draw PRNG → bitflip_rate_ppm / 1_000_000 probability → flip one bit
3. Delay:   sleep latency_ns + uniform_random(0, jitter_ns)
```

---

## 7. Privilege model

| Operation | Requires |
|---|---|
| `modprobe` / `rmmod` | `CAP_SYS_MODULE` (root or `sudo`) |
| Starting `virtrtlabd` | root (creates `/run/virtrtlab/`, opens wire device) |
| Writing sysfs fault attrs | root or group with write permission |
| Connecting to `/run/virtrtlab/uart0.sock` | Member of `virtrtlab` group |
| Opening `/dev/gpiochipN` | Member of `virtrtlab` group (via udev rule) |
| `virtrtlabctl up` / `down` | root (internally calls `sudo` unless `--no-sudo`) |
| `virtrtlabctl get` / `stats` | Any user (read-only sysfs) |

---

## 8. Milestones

| Milestone | Target deliverables |
|---|---|
| `v0.1.0` | `virtrtlab_core` + `virtrtlab_uart` + `virtrtlab_gpio`, daemon, CLI, sysfs MVP, fault injection, CI tests |
| `v0.2.0` | `virtrtlab_can`, named fault profiles, record/replay |
| `v0.3.0` | Tracepoints, full GitHub Actions CI with artifact collection |

---

## 9. Design decisions

### No control channel on the socket

Fault injection, buffer sizes, and stats are accessed via sysfs — not via the socket. This keeps the daemon simple (raw byte relay only) and lets standard tools like `socat` connect without a custom protocol.

### Per-device sockets instead of a multiplexed global socket

Each `/run/virtrtlab/uartN.sock` maps directly to one UART instance. The simulator does a plain `connect()` with no demultiplexing. This matches what `socat` and raw POSIX tools expect.

### `mode` (record/replay) not in the kernel

Record/replay and policy orchestration are userspace concerns handled by harness scripts. The kernel only exposes primitives (latency, drop, etc.). This avoids kernel complexity for a feature that can be implemented reliably in Python.

### Single active connection per socket

`virtrtlabd` accepts exactly one `connect()` per socket at a time. A second `connect()` is rejected. No observer/tap mode in v0.1.0. On simulator disconnect, stale bytes are flushed and the daemon immediately re-enters `listen()`.
