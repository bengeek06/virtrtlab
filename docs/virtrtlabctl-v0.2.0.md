<!-- SPDX-License-Identifier: CC-BY-4.0 -->

# `virtrtlabctl` — v0.2.0 Draft Additions

This document contains **draft CLI specification** for `v0.2.0` features that are not implemented in `v0.1.x` yet.

It exists to avoid mixing forward-looking interface design with the current stable CLI reference in [virtrtlabctl.md](virtrtlabctl.md).

The source of truth for the simulator runtime model remains [simulator-contract-v0.2.0.md](simulator-contract-v0.2.0.md).

---

## 1. `sim` command family

The planned `v0.2.0` simulator-management surface is:

```text
virtrtlabctl sim list [--type TYPE] [--verbose]
virtrtlabctl sim inspect <name>
virtrtlabctl sim attach <device> <simulator> [--auto-start] [--set key=value ...]
virtrtlabctl sim detach <device>
virtrtlabctl sim start <device>
virtrtlabctl sim stop <device>
virtrtlabctl sim status [<device>]
virtrtlabctl sim logs <device> [--stderr] [--tail N] [--follow]
```

### `sim list`

Lists catalog entries visible after precedence resolution.

```sh
virtrtlabctl sim list
virtrtlabctl sim list --type uart
virtrtlabctl sim list --verbose
```

Default output example:

```text
loopback             supports=uart         Echo bytes back to the same VirtRTLab link
ublox-m8-nmea        supports=uart         Simulated u-blox GPS speaking NMEA
```

Verbose output additionally shows the source file that defined each entry and whether it overrides a lower-precedence catalog definition.

### `sim inspect`

Displays full metadata for one simulator entry.

```sh
virtrtlabctl sim inspect loopback
```

Example output:

```text
name            loopback
supports        uart
summary         Echo bytes back to the same VirtRTLab point-to-point link
catalog file    /usr/share/virtrtlab/simulators.d/loopback.toml
restart policy  never

parameters:
  delay_ms      type=u32   required=no   default=0
```

### `sim attach`

Attaches a catalog simulator to one VirtRTLab device without starting it.

```sh
virtrtlabctl sim attach uart0 loopback
virtrtlabctl sim attach uart1 ublox-m8-nmea --auto-start --set scenario_file=./scenarios/static-fix.toml
```

Behaviour:

- validates that the target device exists
- validates that the simulator exists and supports the device type
- validates every `--set key=value` assignment against the simulator parameter declarations
- writes runtime attachment state under the VirtRTLab runtime directory
- does not start the simulator process automatically unless the command is later combined with `sim start` or a profile-driven `up`

### `sim detach`

Removes the attachment from a device.

```sh
virtrtlabctl sim detach uart0
```

If a simulator is currently running for the device, it is stopped first.

### `sim start`

Starts the simulator attached to one device.

```sh
virtrtlabctl sim start uart0
```

Behaviour:

- launches the simulator using its catalog `exec` and `args`
- passes runtime context through the environment variables defined in [simulator-contract-v0.2.0.md](simulator-contract-v0.2.0.md)
- marks the attachment `running` once the process has been spawned successfully and has not exited immediately
- does not wait for a simulator-specific readiness probe in `v0.2.0`

### `sim stop`

Stops the simulator attached to one device.

```sh
virtrtlabctl sim stop uart0
```

Behaviour:

- sends a graceful termination signal first
- force-kills the process if it does not exit within a bounded timeout
- keeps the attachment definition and leaves it in `stopped` state

### `sim status`

Displays attachment state for one device or all attached devices.

```sh
virtrtlabctl sim status
virtrtlabctl sim status uart0
```

Default output example:

```text
uart0   simulator=loopback       state=running  pid=14523  auto_start=yes
uart1   simulator=ublox-m8-nmea  state=attached pid=-      auto_start=no
```

Detailed single-device output example:

```text
device          uart0
simulator       loopback
state           running
pid             14523
auto_start      yes
config file     /run/virtrtlab/simulators/uart0/config.toml
log dir         /run/virtrtlab/simulators/uart0/logs
```

Runtime state is stored under `/run/virtrtlab/simulators/`; see [simulator-contract-v0.2.0.md](simulator-contract-v0.2.0.md) for the exact layout.

### `sim logs`

Prints managed logs for the simulator attached to one device.

```sh
virtrtlabctl sim logs uart0
virtrtlabctl sim logs uart0 --stderr
virtrtlabctl sim logs uart0 --tail 50
virtrtlabctl sim logs uart0 --follow
```

Default behaviour reads the attachment stdout log. `--stderr` selects the stderr log instead.

`--follow` behaves like `tail -f`: it keeps the command attached to the selected log file and streams appended lines until interrupted.

`--tail N --follow` is allowed and starts by printing the last `N` lines before switching to follow mode.

### Built-in `loopback`

`loopback` is the mandatory built-in simulator for `v0.2.0`.

It exists to:

- validate the end-to-end UART path quickly
- exercise the catalog and lifecycle model without protocol complexity
- serve as the reference simulator for third-party authors

In `v0.2.0`, `loopback` is specified as a UART-only simulator with one optional parameter:

```toml
[[parameters]]
name = "delay_ms"
type = "u32"
required = false
default = 0
description = "Delay before echoing received bytes back to the AUT"
```

### Exit codes for `sim` commands

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Operational error (spawn failure, stop timeout, filesystem error) |
| 2 | Invalid arguments or invalid `--set key=value` syntax |
| 3 | State conflict (already running, already attached, already starting) |
| 4 | Not found or incompatible target (unknown simulator, unknown device, unsupported device type, no attachment) |

---

## 2. Draft profile additions

The current `v0.1.x` lab profile supports only device and bus configuration.

The following `v0.2.0` extension is planned for simulator attachments:

```toml
[[devices]]
type = "uart"
count = 2

[[attachments]]
device = "uart0"
simulator = "loopback"
auto_start = true

[attachments.config]
delay_ms = 0
```

`virtrtlabctl up --config ...` will validate `[[attachments]]` before partial startup.
Only attachments with `auto_start = true` are started automatically.