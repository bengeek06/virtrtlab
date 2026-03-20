<!-- SPDX-License-Identifier: CC-BY-4.0 -->

# VirtRTLab AUT Device Contract (v1)

Source of truth is the root [README.md](../README.md). This file defines the
runtime contract between VirtRTLab and an application under test (AUT).

## Overview

VirtRTLab follows one rule for AUT integration:

**same binary, different environment**.

The AUT must not be rebuilt to switch between real hardware and simulation.
VirtRTLab is selected at runtime via environment variables, command-line
arguments, or project-local configuration.

Compile-time switches such as `#ifdef VIRTRTLAB` are out of scope for the v1
contract.

## Contract summary

| Surface | Canonical path | Consumer | Stability |
|---|---|---|---|
| UART AUT-facing | `/dev/ttyVIRTLAB<N>` | AUT | stable across `v0.1.x` |
| UART daemon-facing | `/dev/virtrtlab-wire<N>` | `virtrtlabd` only | internal to VirtRTLab |
| UART simulator socket | `/run/virtrtlab/uart<N>.sock` | simulator | stable across `v0.1.x` |
| GPIO AUT-facing chardev | `/dev/gpiochip<M>` | AUT | dynamic host-assigned index |
| GPIO AUT-facing legacy sysfs | `/sys/class/gpio/gpio<base+L>/` | AUT | dynamic host-assigned base |
| GPIO VirtRTLab control | `/sys/kernel/virtrtlab/devices/gpio<N>/` | CLI / harness | stable across `v0.1.x` |

`N` is the VirtRTLab device instance index (`uart0`, `gpio0`, ...). `M` is the
gpiolib-assigned gpiochip index, which is not guaranteed to be stable across
hosts or boots. `L` is the line offset within the bank (`0..7`).

## Environment variables

### UART

| Name | Consumer | Type | Example value | Error behaviour |
|---|---|---|---|---|
| `VIRTRTLAB_UART<N>` | AUT | absolute path | `/dev/ttyVIRTLAB0` | if present but empty, unreadable, or missing on disk: fail fast |

### GPIO

| Name | Consumer | Type | Example value | Error behaviour |
|---|---|---|---|---|
| `VIRTRTLAB_GPIOCHIP<N>` | AUT using GPIO chardev API | absolute path | `/dev/gpiochip4` | if present but empty, unreadable, or missing on disk: fail fast |
| `VIRTRTLAB_GPIOBASE<N>` | AUT using legacy sysfs GPIO ABI | unsigned integer | `200` | optional export; if present but not an integer: fail fast |
| `VIRTRTLAB_GPIOCTRL<N>` | CLI / harness / diagnostics | absolute path | `/sys/kernel/virtrtlab/devices/gpio0` | if present but invalid: fail fast |

## Resolution rules

| Condition | Behaviour |
|---|---|
| Variable absent | fall back to the production hardware path configured by the AUT |
| Variable present but empty | configuration error; do not silently fall back |
| Variable present but path does not exist | configuration error |
| Variable present but permission denied | configuration error |
| Multiple VirtRTLab instances | variables are indexed by the VirtRTLab device index (`0`, `1`, ...) |

Presence of a `VIRTRTLAB_*` variable is an explicit request to use the
simulation path. Falling back silently to real hardware in that case would hide
CI misconfiguration.

## GPIO integration modes

VirtRTLab supports both standard Linux GPIO userspace interfaces for AUTs.

### Mode 1 - GPIO character device API

The AUT uses `libgpiod` or the GPIO v2 ioctl API on `/dev/gpiochip<M>`.

Mapping rule:

| VirtRTLab device | AUT-facing handle | Discovery |
|---|---|---|
| `gpio0` | `/dev/gpiochip<M>` | `VIRTRTLAB_GPIOCHIP0` or `chip_path` |
| `gpio1` | `/dev/gpiochip<M>` | `VIRTRTLAB_GPIOCHIP1` or `chip_path` |

### Mode 2 - Legacy sysfs GPIO ABI

The AUT uses `/sys/class/gpio/export`, `direction`, `value`, and `edge`.

Mapping rule:

| VirtRTLab device | Base | Line 0 | Line 7 |
|---|---|---|---|
| `gpio0` | `VIRTRTLAB_GPIOBASE0` | `gpio<base+0>` | `gpio<base+7>` |
| `gpio1` | `VIRTRTLAB_GPIOBASE1` | `gpio<base+0>` | `gpio<base+7>` |

Example: if `VIRTRTLAB_GPIOBASE0=200`, then line `3` of `gpio0` is exposed as
`/sys/class/gpio/gpio203/`.

If the host kernel does not expose the legacy `/sys/class/gpio` ABI,
`VIRTRTLAB_GPIOBASE<N>` is omitted from the VirtRTLab contract. This is a
degraded-but-supported configuration, not a startup error.

### Mode 3 - VirtRTLab harness control

The harness and `virtrtlabctl` use the VirtRTLab-specific control plane:

| VirtRTLab device | Control root |
|---|---|
| `gpio0` | `/sys/kernel/virtrtlab/devices/gpio0/` |
| `gpio1` | `/sys/kernel/virtrtlab/devices/gpio1/` |

This plane is used for:

- fault injection
- stats collection
- diagnostic discovery
- deterministic CI setup

It is not a replacement for the AUT-facing Linux GPIO APIs.

## `virtrtlabctl up` contract

`virtrtlabctl up` must print the resolved AUT integration contract in both
human-readable and JSON forms.

### Human output

```text
[ok] uart0 loaded
     tty: /dev/ttyVIRTLAB0
     export VIRTRTLAB_UART0=/dev/ttyVIRTLAB0

[ok] gpio0 loaded
     gpiochip: /dev/gpiochip4
     sysfs base: 200
     control: /sys/kernel/virtrtlab/devices/gpio0
     export VIRTRTLAB_GPIOCHIP0=/dev/gpiochip4
     export VIRTRTLAB_GPIOBASE0=200
     export VIRTRTLAB_GPIOCTRL0=/sys/kernel/virtrtlab/devices/gpio0
```

  When the legacy sysfs GPIO ABI is unavailable on the host:

  ```text
  [ok] gpio0 loaded
    gpiochip: /dev/gpiochip4
    control: /sys/kernel/virtrtlab/devices/gpio0
    export VIRTRTLAB_GPIOCHIP0=/dev/gpiochip4
    export VIRTRTLAB_GPIOCTRL0=/sys/kernel/virtrtlab/devices/gpio0
    [warn] legacy sysfs GPIO ABI unavailable; VIRTRTLAB_GPIOBASE0 omitted
  ```

### JSON output

```json
{
  "devices": [
    {
      "name": "uart0",
      "type": "uart",
      "aut_path": "/dev/ttyVIRTLAB0",
      "wire_path": "/dev/virtrtlab-wire0",
      "socket_path": "/run/virtrtlab/uart0.sock",
      "env": {
        "VIRTRTLAB_UART0": "/dev/ttyVIRTLAB0"
      }
    },
    {
      "name": "gpio0",
      "type": "gpio",
      "chip_path": "/dev/gpiochip4",
      "sysfs_base": 200,
      "control_path": "/sys/kernel/virtrtlab/devices/gpio0",
      "env": {
        "VIRTRTLAB_GPIOCHIP0": "/dev/gpiochip4",
        "VIRTRTLAB_GPIOBASE0": "200",
        "VIRTRTLAB_GPIOCTRL0": "/sys/kernel/virtrtlab/devices/gpio0"
      }
    }
  ]
}
```

If the legacy sysfs GPIO ABI is unavailable, `sysfs_base` is omitted and a
warning is included:

```json
{
  "devices": [
    {
      "name": "gpio0",
      "type": "gpio",
      "chip_path": "/dev/gpiochip4",
      "control_path": "/sys/kernel/virtrtlab/devices/gpio0",
      "warnings": [
        "legacy sysfs GPIO ABI unavailable; VIRTRTLAB_GPIOBASE0 omitted"
      ],
      "env": {
        "VIRTRTLAB_GPIOCHIP0": "/dev/gpiochip4",
        "VIRTRTLAB_GPIOCTRL0": "/sys/kernel/virtrtlab/devices/gpio0"
      }
    }
  ]
}
```

## AUT examples

### UART

```c
const char *uart0 = getenv("VIRTRTLAB_UART0");
if (!uart0)
    uart0 = "/dev/ttyS0";

int fd = open(uart0, O_RDWR);
```

### GPIO chardev

```c
const char *chip0 = getenv("VIRTRTLAB_GPIOCHIP0");
if (!chip0)
  chip0 = configured_prod_gpiochip_path;
```

### GPIO legacy sysfs

```c
const char *base_str = getenv("VIRTRTLAB_GPIOBASE0");
int base = base_str ? atoi(base_str) : configured_prod_gpio_base;
```

In both examples above, the fallback comes from the AUT's normal production
configuration. VirtRTLab does not guarantee a fixed host gpiochip index or a
fixed legacy sysfs GPIO base.

## Rationale

**Why keep `/dev/ttyVIRTLAB<N>` for UART?**
The UART AUT-facing path is already established across the repository and maps to
the TTY semantics expected by serial AUTs. The wire device is a daemon-internal
transport and must not become part of the AUT contract.

**Why support both GPIO APIs?**
Existing AUTs use both Linux GPIO families in the field: newer code tends to use
`libgpiod` and `/dev/gpiochip*`, but older AUTs still rely on `/sys/class/gpio`.
VirtRTLab cannot require test adopters to rewrite their GPIO layer just to use
the simulator.

**Why not guarantee `/dev/gpiochip0` or a fixed sysfs base?**
Linux assigns gpiochip indices and legacy sysfs base numbers dynamically. The
stable contract is therefore the exported mapping (`VIRTRTLAB_GPIOCHIP<N>`,
`VIRTRTLAB_GPIOBASE<N>`) rather than a hard-coded host path.

## Open questions

> **Open:** if a future lab profile explicitly requests the legacy sysfs GPIO
> AUT interface, should absence of `/sys/class/gpio` become a hard startup
> error for that profile? The default v1 behaviour remains degraded success:
> `VIRTRTLAB_GPIOBASE<N>` is omitted and the rest of the contract is exported.