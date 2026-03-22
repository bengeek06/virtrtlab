<!-- SPDX-License-Identifier: CC-BY-4.0 -->

# VirtRTLab Architecture and Interface Specification (v0.2.0 Draft)

This document is the source of truth for the `v0.2.0` architecture.

It preserves the naming and device-model goals of VirtRTLab while changing one
core assumption from `v0.1`: device instances are now expected to be dynamic and
the daemon owns the canonical write-side control plane.

## 1. Architectural summary

VirtRTLab `v0.2.0` is built around three planes:

| Plane | Surface | Role |
|---|---|---|
| AUT plane | `/dev/ttyVIRTLABN`, `/dev/gpiochipM`, other device nodes | the AUT talks to simulated peripherals through standard Linux interfaces |
| Data plane | `/run/virtrtlab/devices/<device>.sock` | simulator-facing per-device dataplane |
| Control plane | `/run/virtrtlab/control.sock` | topology, faults, stats, simulator lifecycle |

Architectural boundary:

- the AUT configures the simulated controller through the same Linux interfaces it would use on real hardware
- the simulator exchanges device dataplane payloads through the per-device socket
- the CLI and harnesses use the control plane for topology, stats, and fault injection

## 2. Naming conventions

| Object | Convention | Example |
|---|---|---|
| Device type | lowercase ASCII | `uart`, `gpio` |
| Device instance | `<type><N>` | `uart0`, `gpio0` |
| Control socket | fixed path | `/run/virtrtlab/control.sock` |
| Device data socket | `/run/virtrtlab/devices/<device>.sock` | `/run/virtrtlab/devices/uart0.sock` |

## 3. Device lifecycle model

In `v0.2.0`, device instances are runtime objects.

| Operation | Meaning |
|---|---|
| create | instantiate one new device without reloading the driver module |
| destroy | remove one existing device without unloading sibling instances |
| reset | restore one existing device to its documented reset baseline |

The module-load boundary is no longer the normal configuration boundary.

## 4. Control-plane model

The daemon is the canonical write-side controller.

Normative references:

- [control-socket-v0.2.0.md](control-socket-v0.2.0.md)
- [daemon-v0.2.0.md](daemon-v0.2.0.md)
- [privilege-model-v0.2.0.md](privilege-model-v0.2.0.md)

Read-side sysfs observability remains part of the platform contract.

## 5. Driver integration model

Each VirtRTLab-compatible driver must satisfy:

- the dynamic device-lifecycle contract in
  [driver-contract-v0.2.0.md](driver-contract-v0.2.0.md)
- the control-plane capability discovery expectations
- stable naming and resolved path reporting

## 6. Simulator integration model

Simulator discovery, attachment, and runtime context remain defined by:

- [simulator-contract-v0.2.0.md](simulator-contract-v0.2.0.md)
- [virtrtlabctl-v0.2.0.md](virtrtlabctl-v0.2.0.md)

The key architectural rule is now:

**simulators attach to dynamic VirtRTLab device instances that are created and
managed through the daemon control plane.**

## 7. Privilege model summary

| Operation class | Canonical path |
|---|---|
| lab topology changes | control socket |
| persistent fault configuration | control socket |
| one-shot injection | control socket |
| simulator lifecycle | control socket |
| runtime diagnostics | control socket or read-side sysfs |

## 8. Milestone focus

The `v0.2.0` architecture now includes the following cross-cutting themes:

| Theme | Meaning |
|---|---|
| hotpluggable device instances | no routine `insmod`/`rmmod` during test sequencing |
| daemonized control plane | one canonical non-root control surface |
| simulator orchestration | attachments, catalog discovery, lifecycle state |
| driver contract | standard integration rules for future VirtRTLab-compatible drivers |

## 9. Rationale

**Why version the architecture doc instead of editing `spec.md`?**  
Because `spec.md` still describes the observable world of the current
implementation. `v0.2.0` needs a separate architectural source of truth until the
new model is implemented.

**Why treat dynamic topology as an architectural concern?**  
Once devices are created and destroyed at runtime, topology affects privilege,
control sequencing, simulator state, and driver design. It is no longer an
implementation detail of module parameters.

## 10. Decision

`v0.2.0` keeps one shared kernel-visible VirtRTLab aggregation object for
discovery and diagnostics.

The architecture therefore standardizes two distinct layers:

- one common, kernel-visible VirtRTLab aggregation point used for discovery,
  observability, and coherent documentation
- driver-specific internal implementation details that remain unconstrained as
  long as the observable contract stays stable

The aggregation object may continue to appear as a shared bus or equivalent
common hierarchy anchor such as `vrtlbus0`.