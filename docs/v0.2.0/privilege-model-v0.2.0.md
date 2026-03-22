<!-- SPDX-License-Identifier: CC-BY-4.0 -->

# VirtRTLab Privilege Model (v0.2.0 Draft)

This document defines the observable privilege model for the `v0.2.0` control
plane.

It supersedes the `v0.1` assumption that routine control is performed directly
through writable sysfs attrs or repeated privileged module lifecycle commands.

## 1. Overview

VirtRTLab `v0.2.0` follows this rule:

**routine lab control is non-root and daemon-mediated.**

The daemon holds the narrow privileged path required to materialize and destroy
kernel-backed device instances. Operators and CI jobs interact with the daemon
through the control socket.

Canonical group name: `virtrtlab`

Canonical service account: `virtrtlab`

## 2. Actors

| Actor | Typical identity | Allowed operations | Not allowed |
|---|---|---|---|
| Installer | root | install files, create accounts, enable service integration | normal day-to-day control is not the install path |
| Lab user | member of `virtrtlab` | connect to the control socket, run `virtrtlabctl`, access device nodes and data sockets, inject faults | arbitrary root shell operations |
| Daemon service | user `virtrtlab`, group `virtrtlab`, with a narrow privileged execution context configured by the service manager | create and destroy VirtRTLab devices, supervise simulators, manage runtime files | unrestricted root session |
| CI runner | user or service account in `virtrtlab` | run repeated `up`, `down`, simulator lifecycle, and fault injection without interactive privilege escalation | direct unrestricted kernel module management |

## 3. Access surfaces

| Surface | Path pattern | Owner/group contract | Mode contract | Primary consumer |
|---|---|---|---|---|
| Control socket | `/run/virtrtlab/control.sock` | `virtrtlab:virtrtlab` | `0660` | CLI, CI harnesses, diagnostics |
| Data sockets | `/run/virtrtlab/devices/*.sock` | `virtrtlab:virtrtlab` | `0660` | simulator processes |
| Runtime directory | `/run/virtrtlab/` | `virtrtlab:virtrtlab` | `0750` | daemon and group members |
| AUT-facing device nodes | VirtRTLab-owned `/dev/*` endpoints | group `virtrtlab` | `0660` | AUT binaries and harnesses |
| Sysfs read-only attrs | `/sys/kernel/virtrtlab/**` | readable without group write ownership | `0444` | diagnostics |

## 4. Control path policy

### 4.1 Canonical write-side control

The following operations must be performed through the daemon control socket in
the normal installed profile:

- lab up/down/reconfigure
- device create/destroy
- device attribute writes
- stats reset
- one-shot fault injection
- simulator attach/start/stop/detach

### 4.2 Sysfs write policy

Writable sysfs attrs may remain available for debugging or compatibility, but
they are not the normative privilege path in `v0.2.0`.

Rules:

- unprivileged automation must not depend on direct sysfs writes
- if writable sysfs attrs exist, their access policy may be stricter than the control socket policy
- the daemon remains responsible for presenting stable operator-facing errors and validation

## 5. Daemon privilege contract

| Property | Required behavior | Error behavior |
|---|---|---|
| Identity | runs as user `virtrtlab`, group `virtrtlab` | service start fails if the account cannot be resolved |
| Privileged path | obtains only the capabilities or service-manager privileges needed to create and destroy VirtRTLab device instances | service start fails if the service manager cannot apply the configured policy |
| Runtime ownership | creates sockets and runtime files with group-visible permissions | clients receive normal `EACCES` or connect failures if the daemon is absent or misconfigured |
| Root policy | must not require an unrestricted root shell for routine steady-state operation | configuration is invalid if the installed profile depends on full-root daemon operation |

The exact service-manager mechanism is distribution-specific. The normative
contract is the outcome: a long-running daemon that can perform the narrow
device-lifecycle operations required by VirtRTLab without exposing a general
root control surface to lab users.

## 6. CLI privilege contract

After installation and group-membership setup, the following commands must
succeed for a `virtrtlab` group member without interactive `sudo`:

| Command family | Expected privilege path |
|---|---|
| `up`, `down`, `status` | control socket |
| `list`, `get`, `set`, `reset`, `stats`, `inject` | control socket |
| `sim *` | control socket for lifecycle, data sockets for simulator processes |

If the daemon is unavailable, the CLI must fail with a normal daemon-unavailable
error. It must not silently fall back to direct privileged module commands in
the installed profile.

## 7. Simulator process privilege contract

Managed simulator processes are started by the daemon or by a daemon-authorized
supervisor path.

Required outcomes:

- the simulator has access to the target data socket without additional privilege escalation
- the simulator does not require root by default
- simulator-specific extra privileges, if any, are declared by that simulator and are outside the base VirtRTLab contract

In the installed profile, the daemon is the authoritative owner of managed
simulator lifecycle. `virtrtlabctl` is the operator-facing client of that
daemon-managed lifecycle.

## 8. Root-only operations

The following remain root-only by design:

| Operation | Reason |
|---|---|
| package installation and uninstall | machine-wide state changes |
| initial service enablement and service-manager policy changes | administrative action |
| out-of-band kernel module installation or update | machine-wide kernel integration |

## 9. Error behavior

| Condition | Required behavior |
|---|---|
| user not in `virtrtlab` | connect or open fails with standard permission error |
| daemon absent | control commands fail with daemon/socket error |
| daemon privilege path misconfigured | service start fails; CLI reports daemon unavailable or operational failure |
| direct writable sysfs unavailable | not treated as a control-plane regression if the control socket contract is satisfied |

## 10. Rationale

**Why move routine control away from direct sysfs writes?**  
It gives VirtRTLab one place to validate topology changes, serialize conflicting
operations, and express structured errors to CI and operator tooling.

**Why avoid fallback to `sudo` in the installed profile?**  
The whole point of the `v0.2.0` model is to make test sequencing predictable and
non-interactive. Silent privilege escalation would reintroduce environment-specific
behavior and hard-to-debug CI failures.

## 11. Decisions

- the daemon does not stop or restart itself as part of routine `v0.2.0`
	operation; hotplug-capable device lifecycle removes the need for daemon
	self-restart in the normal model
- in the installed profile, managed simulator lifecycle is daemon-owned for
	uniformity; the CLI does not become an alternative execution authority