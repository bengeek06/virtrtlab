<!-- SPDX-License-Identifier: CC-BY-4.0 -->

# virtrtlabd Configuration (v0.2.0 Draft)

This document defines the external configuration contract of `virtrtlabd` in
`v0.2.0`.

It exists because the daemon now owns the control plane, device dataplane
socket layout, runtime-state files, and simulator lifecycle behavior. Those
surfaces require deployment-time configuration that should not be hard-coded.

## 1. Scope

The daemon configuration contract covers:

- configuration file presence and format
- configurable filesystem paths
- logging configuration
- transport-selection policy for the control and dataplane sockets

It does not cover:

- per-device simulator catalog metadata
- lab profile content
- kernel driver capability discovery

## 2. Format and location

In `v0.2.0`, the daemon configuration format is **TOML**.

Canonical search order:

| Precedence | Path | Purpose |
|---|---|---|
| 1 | explicit `--config <path>` CLI argument | operator-selected daemon config |
| 2 | `/etc/virtrtlab/virtrtlabd.toml` | installed system-wide daemon config |
| 3 | built-in defaults | no external file present |

If the selected config file exists but is malformed, daemon startup fails.

Configuration snippets in this document are illustrative examples. Normative
requirements are defined by the key tables and rule lists that accompany them.

## 3. Top-level sections

The following top-level sections are defined in `v0.2.0`:

| Section | Purpose |
|---|---|
| `[runtime]` | runtime directory, pid file, and state-file placement |
| `[control]` | control-socket transport and path |
| `[dataplane]` | per-device data-socket defaults |
| `[logging]` | daemon log behavior |

## 4. Runtime section

Illustrative example:

```toml
[runtime]
root_dir = "/run/virtrtlab"
pid_file = "/run/virtrtlab/virtrtlabd.pid"
state_dir = "/run/virtrtlab/state"
simulator_dir = "/run/virtrtlab/simulators"
```

| Key | Type | Default | Meaning |
|---|---|---|---|
| `root_dir` | string | `/run/virtrtlab` | base runtime directory |
| `pid_file` | string | `/run/virtrtlab/virtrtlabd.pid` | daemon pid file path |
| `state_dir` | string | `/run/virtrtlab/state` | daemon-owned aggregate state directory for non-simulator runtime metadata |
| `simulator_dir` | string | `/run/virtrtlab/simulators` | per-attachment simulator runtime-state and log root |

Rules:

- relative paths are not allowed
- all configured paths must remain within one local filesystem namespace visible to the daemon
- startup fails if the daemon cannot create or access the configured runtime paths
- clients and companion documents must treat these paths as resolved runtime values,
  not as immutable hard-coded locations
- `state_dir` and `simulator_dir` are intentionally distinct so simulator lifecycle files do not become the implicit storage location for unrelated daemon runtime state

## 5. Control section

Illustrative example:

```toml
[control]
transport = "unix"
socket_path = "/run/virtrtlab/control.sock"
```

| Key | Type | Default | Meaning |
|---|---|---|---|
| `transport` | string | `unix` | control-plane transport kind |
| `socket_path` | string | `/run/virtrtlab/control.sock` | Unix-socket path when `transport = "unix"` |

`transport` allowed values in `v0.2.0`:

| Value | Status |
|---|---|
| `unix` | required and normative |

No network transport is part of the `v0.2.0` base contract. Future revisions may
add additional transport kinds, but `unix` remains the only mandatory one.

## 6. Dataplane section

Illustrative example:

```toml
[dataplane]
transport = "unix"
socket_dir = "/run/virtrtlab/devices"
name_pattern = "{device}.sock"
```

| Key | Type | Default | Meaning |
|---|---|---|---|
| `transport` | string | `unix` | dataplane transport kind |
| `socket_dir` | string | `/run/virtrtlab/devices` | directory for per-device dataplane sockets |
| `name_pattern` | string | `{device}.sock` | socket file naming template |

Rules:

- `transport = "unix"` is the only required dataplane transport in `v0.2.0`
- `{device}` expands to the canonical VirtRTLab device name such as `uart0` or `gpio0`
- the daemon must expose the resolved dataplane path through the control plane for every device that supports a data socket
- examples under `/run/virtrtlab/...` are the default installed layout, not the
  only valid deployed layout

Illustrative resolved paths:

| Device | Result |
|---|---|
| `uart0` | `/run/virtrtlab/devices/uart0.sock` |
| `gpio0` | `/run/virtrtlab/devices/gpio0.sock` |

## 7. Logging section

Illustrative example:

```toml
[logging]
destination = "journald"
level = "info"
stderr = false
```

| Key | Type | Default | Meaning |
|---|---|---|---|
| `destination` | string | `journald` | primary logging backend |
| `level` | string | `info` | minimum emitted severity |
| `stderr` | bool | `false` | whether to mirror logs to stderr |

Allowed `destination` values in `v0.2.0`:

| Value | Meaning |
|---|---|
| `journald` | systemd journal or syslog-compatible service-manager sink |
| `stderr` | foreground process stderr |

Allowed `level` values:

`debug`, `info`, `warn`, `error`

## 8. Minimal example

```toml
[runtime]
root_dir = "/run/virtrtlab"

[control]
transport = "unix"
socket_path = "/run/virtrtlab/control.sock"

[dataplane]
transport = "unix"
socket_dir = "/run/virtrtlab/devices"
name_pattern = "{device}.sock"

[logging]
destination = "journald"
level = "info"
stderr = false
```

## 9. Rationale

**Why add a daemon config file now?**  
The daemon is no longer a trivial relay process. Runtime paths, socket layout,
and logging behavior are deployment concerns that need a documented contract.

**Why keep Unix sockets mandatory in `v0.2.0`?**  
They provide the simplest local security model and keep the control plane out of
scope of network exposure concerns for the first hotplug-capable release.

## 10. Decision

One common `{device}.sock` naming rule is sufficient in `v0.2.0`.

Per-device-type dataplane naming templates are out of scope for the first
release and may be introduced only if a concrete deployment need appears.

The runtime paths defined in this document are normative deployment outputs for
the daemon and simulator lifecycle surfaces they govern. They do not create a
second topology API beyond the control socket.