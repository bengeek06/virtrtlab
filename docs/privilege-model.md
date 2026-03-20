<!-- SPDX-License-Identifier: CC-BY-4.0 -->

# VirtRTLab Privilege Model (v1)

Source of truth is the root [README.md](../README.md). This document defines the
observable privilege model for installed VirtRTLab systems.

## Overview

VirtRTLab follows a least-privilege model:

- day-to-day users do not need full root access after installation
- routine CLI use is granted through group membership and device/sysfs ownership
- privileged module lifecycle operations are mediated by the daemon or by a
  development-only allowlist
- installation and system integration steps remain root-only by design

Canonical group name: `virtrtlab`

Canonical service account: `virtrtlab`

## Actors

| Actor | Typical identity | Allowed operations | Not allowed |
|---|---|---|---|
| Installer | root | install files, create accounts, install udev/systemd/sudoers integration | normal AUT use as an unprivileged user is not the install path |
| Lab user | member of group `virtrtlab` | use `virtrtlabctl`, access VirtRTLab device nodes, read/write allowed sysfs attrs, run AUT binaries against VirtRTLab devices | system-wide install/uninstall, unmanaged root shell operations |
| Daemon service | user `virtrtlab`, group `virtrtlab` | mediate module lifecycle and create runtime socket/files | broad root privileges |
| Development/CI runner | group member, optionally with targeted sudoers entry | load/unload modules through the allowlisted development path, collect logs | unrestricted root access |

## Install Profiles

| Profile | Installed for | Required privilege at install time | Runtime outcome |
|---|---|---|---|
| `make install` | normal system use | root | users in group `virtrtlab` can use the CLI and AUT-facing device/sysfs surfaces without interactive `sudo` |
| `make install-dev` | local development and CI hosts | root | same as `make install`, plus targeted module-management permissions for the development/test path |

`make install-dev` extends `make install`; it does not replace it.

## Post-install setup

The installation contract must provide the following setup steps for human users:

```sh
sudo usermod -aG virtrtlab $USER
newgrp virtrtlab
```

Logging out and back in is equivalent to `newgrp virtrtlab`.

If the current user is not a member of `virtrtlab`, VirtRTLab commands that rely
on group-owned sysfs or device nodes must fail with the underlying permission
error rather than silently retrying as root.

## Installed system identities

| Object | Required state | Error behaviour |
|---|---|---|
| Group | system group `virtrtlab` exists | installation fails if the group cannot be created or resolved |
| Service user | system user `virtrtlab` exists and belongs to group `virtrtlab` | installation fails if the user cannot be created or resolved |
| Runtime directory | `/run/virtrtlab/` exists when the daemon is active | CLI commands depending on the daemon return their documented daemon/socket error |

Creation must be idempotent: re-running installation must not fail solely because
the group or service user already exists.

## Access surfaces

### Access matrix

| Surface | Path pattern | Owner/group contract | Mode contract | Primary consumer | Error behaviour |
|---|---|---|---|---|---|
| Sysfs read-only attrs | `/sys/kernel/virtrtlab/**` | readable without group ownership | `0444` | users, harnesses, diagnostics | reads fail with standard kernel/sysfs errors if the path is absent |
| Sysfs read-write attrs | `/sys/kernel/virtrtlab/**` writable attrs | group `virtrtlab` | `0664` | users, harnesses, CLI | non-members get `EACCES`/`EPERM`; kernel validation errors remain unchanged |
| AUT-facing device nodes | `/dev/ttyVIRTLAB<N>`, `/dev/gpiochip<M>`, other VirtRTLab-owned nodes | group `virtrtlab` | `0660` | AUT binaries, users, harnesses | non-members get `EACCES`/`EPERM` from the device node access |
| Daemon runtime socket | `/run/virtrtlab/*.sock` | group `virtrtlab` | `0660` | simulator, CLI if applicable, test harnesses | non-members get `EACCES`/`EPERM`; missing daemon yields normal connect/open failures |
| Daemon runtime directory | `/run/virtrtlab/` | service user/group `virtrtlab` | `0750` | daemon, group members traversing to socket files | non-members cannot traverse the directory |

### Writable sysfs attrs

The following rule applies to every writable VirtRTLab sysfs attribute:

| Class | Access | Mode | Group policy |
|---|---|---|---|
| Fault attrs (`enabled`, `latency_ns`, `jitter_ns`, `drop_rate_ppm`, `bitflip_rate_ppm`) | rw | `0664` | writable by group `virtrtlab` |
| Control attrs (`inject`, `stats/reset`, bus `state`, bus `seed`) | rw / wo | `0664` | writable by group `virtrtlab` |

VirtRTLab must not require full root privileges for routine fault injection or
stats reset after installation.

## Device and socket ownership rules

### Device nodes

| Node family | Group | Mode | Intended effect |
|---|---|---|---|
| UART TTY nodes | `virtrtlab` | `0660` | AUT serial clients run without `sudo` |
| VirtRTLab-owned control/wire char devices | `virtrtlab` | `0660` | daemon, harnesses, and diagnostics run without `sudo` |

### Socket endpoints

| Object | Group | Mode | Intended effect |
|---|---|---|---|
| per-device daemon sockets under `/run/virtrtlab/` | `virtrtlab` | `0660` | simulator and test harness access without `sudo` |

## CLI privilege contract

After a successful `make install` and group membership setup, the following CLI
operations must succeed for a `virtrtlab` group member without interactive `sudo`:

| Command family | Expected privilege path |
|---|---|
| `list`, `get`, `stats`, `status` | direct access to readable sysfs/runtime files |
| `set`, `reset` | direct access to group-writable sysfs attrs |
| `up`, `down` | daemon mediation in the normal install profile, or development-only allowlist in the development profile |

`--no-sudo` remains a supported flag for environments that already provide the
required privileges through service mediation, targeted sudoers integration, or
an existing privileged shell.

## Daemon service contract

The installed daemon service must satisfy all of the following:

| Property | Required behaviour | Error behaviour |
|---|---|---|
| Identity | runs as user `virtrtlab`, group `virtrtlab` | service start fails if the account cannot be resolved |
| Capability set | limited to `CAP_SYS_MODULE` for the privileged module-management path | service start fails if the service manager cannot apply the capability policy |
| Root policy | must not run as unrestricted root during normal service operation | configuration is invalid if full-root operation is required in the installed profile |
| Runtime files | creates runtime files under `/run/virtrtlab/` with group-visible permissions | socket/client operations fail with normal runtime errors if the service is absent |

## Development and CI contract

`make install-dev` may install a targeted privilege allowlist for development and
CI environments.

### Allowlisted operations

| Operation family | Scope |
|---|---|
| module load | VirtRTLab modules only |
| module unload | VirtRTLab modules only |
| kernel log collection | diagnostic commands needed by the automated tests |

The development profile must not grant blanket root access beyond those targeted
operations.

### Test suite expectations

| Context | Required gating rule |
|---|---|
| local developer without group membership | tests that require VirtRTLab access skip with guidance to join `virtrtlab` |
| local developer with group membership | tests run without interactive `sudo` for routine sysfs/device access |
| CI with development profile | module-management path works through the targeted allowlist |

## Root-only operations

The following remain root-only by design:

| Operation | Reason |
|---|---|
| `make install` / `make uninstall` | writes to system-owned installation paths |
| enabling/disabling the system service | changes machine-wide service state |
| DKMS registration and system-wide packaging hooks | machine-wide kernel integration |

## Rationale

**Why use a dedicated group?**
The group model matches established Linux patterns such as `dialout`, `kvm`, and
`docker`: routine use is delegated to a named group rather than to unrestricted
root access.

**Why keep installation root-only?**
Installation modifies machine-wide state under system directories and service
managers. Those changes are administrative actions, not day-to-day lab use.

**Why distinguish normal install from install-dev?**
Development and CI need a narrower, inspectable privilege path for module load/
unload and diagnostics. Separating the profiles avoids over-privileging normal
runtime users.

**Why require a dedicated service account?**
Running the daemon under a dedicated non-login account narrows the blast radius
of the privileged mediation path and keeps ownership of runtime files predictable.

## Open questions

> **Open:** should the installed profile require all module lifecycle operations
> to go through the daemon exclusively, or may `virtrtlabctl` retain a direct
> privileged fallback when the daemon service is unavailable?

> **Open:** should the exact installation paths for udev rules, systemd units,
> and sudoers fragments be part of the normative cross-distribution contract, or
> should the spec define only the resulting ownership/mode/capability outcomes?
