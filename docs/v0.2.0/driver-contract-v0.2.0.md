<!-- SPDX-License-Identifier: CC-BY-4.0 -->

# VirtRTLab Driver Contract (v0.2.0 Draft)

This document defines the observable contract that a kernel driver must satisfy
to be managed by VirtRTLab in `v0.2.0`.

It is the driver-side counterpart to
[simulator-contract-v0.2.0.md](simulator-contract-v0.2.0.md): one document
defines how userspace simulators plug into VirtRTLab, the other defines how
kernel device models plug into the same control and lifecycle framework.

## 1. Scope

The driver contract covers:

- driver registration into the VirtRTLab control plane
- dynamic creation and destruction of device instances
- identity, discovery, and lifecycle states
- required observability surfaces
- required control operations and error behavior

The driver contract does not cover:

- internal locking strategy inside a driver
- the exact kernel API used to implement dynamic instantiation
- protocol-specific simulator behavior

## 2. Design goals

Every VirtRTLab-compatible driver must satisfy these goals:

- device instances are creatable and removable at runtime without reloading the module
- the daemon can discover capabilities and validate control requests before they reach the device
- read-side observability remains available through stable sysfs naming
- write-side control is mediated by the daemon control socket in the normal installed profile
- AUT-visible controller configuration remains under AUT control through normal Linux interfaces

## 3. Required concepts

### 3.1 Driver type identity

Each compatible driver exposes one stable device type string.

| Property | Rule |
|---|---|
| Character set | lowercase ASCII letters, digits, `-` |
| Examples | `uart`, `gpio`, `can`, `spi` |
| Stability | once published, a type name is stable across the `v0.2.x` line |

### 3.2 Device instance identity

Each runtime instance is named `<type><N>`.

| Example | Meaning |
|---|---|
| `uart0` | first UART instance |
| `gpio3` | fourth GPIO bank instance |

Rules:

- indices are zero-based per device type
- names must remain unique within one daemon instance
- removing one device does not rename surviving siblings

## 4. Mandatory lifecycle operations

Every compatible driver must support the following externally observable operations.

| Operation | Required behavior |
|---|---|
| create | materialize one new device instance at runtime |
| destroy | remove one existing device instance at runtime |
| get | return identity, resolved host paths, and current attribute values |
| set | update writable device attributes atomically per request |
| reset | restore the documented reset baseline for that device |
| stats | expose per-device counters |
| stats_reset | reset per-device counters |

### 4.1 Create

Create must be externally observable as follows:

- the device appears in `device.list`
- all required read-side sysfs nodes become visible before success is reported
- AUT-facing device nodes or paths, if any, are resolved before success is reported

### 4.2 Destroy

Destroy must be externally observable as follows:

- the device disappears from `device.list`
- any per-device simulator attachment is stopped or rejected before destruction completes
- outstanding references held by userspace fail with the documented transport error for that surface

## 5. Mandatory discovery surface

Each device instance must expose the following fields through the daemon control plane.

| Field | Type | Meaning |
|---|---|---|
| `name` | string | device instance name, for example `uart0` |
| `type` | string | device type, for example `uart` |
| `state` | string | lifecycle state visible to userspace |
| `paths` | object | resolved host-facing paths for AUT, simulator, and diagnostics |
| `attrs` | object | current attribute values |
| `stats` | object | current counter values |

`paths` is device-specific. Examples:

| Device type | Required path keys |
|---|---|
| `uart` | `aut_path`, `data_path`, `sysfs_path` |
| `gpio` | `chip_path`, `data_path`, `sysfs_path` |

## 6. Mandatory observability surface

### 6.1 Sysfs

Read-side sysfs observability remains mandatory.

Each compatible driver must expose a stable per-device subtree under:

```text
/sys/kernel/virtrtlab/devices/<device>/
```

Required minimum read-side attributes:

| Attribute | Access | Meaning |
|---|---|---|
| `type` | ro | device type |
| `bus` | ro | owning VirtRTLab bus or lab scope |
| `state` | ro | current lifecycle or enablement state |
| `stats/` | ro subtree plus reset entry | per-device counters |

Additional read-only attributes are device-specific.

### 6.2 Writable sysfs

Writable sysfs attrs are optional compatibility surfaces in `v0.2.0`.

Rules:

- the canonical control contract is the daemon control socket
- if a driver still exposes writable sysfs attrs, their semantics must match the control-socket operations exactly
- scripts and CI harnesses must not rely on writable sysfs attrs as the primary control path in `v0.2.0`

## 7. Mandatory control semantics

### 7.1 Atomicity

For any multi-field update request such as `device.set`, the driver-visible result
must be atomic from userspace:

- either all fields are applied
- or none are applied and the old values remain visible

### 7.2 Validation

Each driver must validate:

- unknown field names
- out-of-range scalar values
- malformed structured payloads
- requests that conflict with the current device state

### 7.3 Reset

Each driver must document a reset baseline.

Minimum reset obligations:

- clear runtime fault injection state unless that state is explicitly documented as persistent across reset
- reset all documented stats counters to zero
- leave device identity and index unchanged
- return the device to an operator-usable state unless the type-specific spec says otherwise

### 7.4 AUT configuration boundary

The driver contract must preserve the AUT configuration boundary.

Rules:

- controller configuration that would exist on a real device remains under AUT control
- the daemon control plane does not replace AUT-facing configuration interfaces such as `termios`, GPIO sysfs, or GPIO `ioctl`
- the simulator-facing dataplane must not be specified as a controller-configuration interface

## 8. Mandatory fault model hooks

A VirtRTLab-compatible driver must declare its supported fault surfaces.

Each supported fault surface must specify:

| Field | Meaning |
|---|---|
| transfer unit | what one probability draw or delay applies to |
| supported persistent attrs | such as `enabled`, `latency_ns`, `jitter_ns`, `drop_rate_ppm`, `bitflip_rate_ppm` |
| supported one-shot injections | such as `line-write` or `rx-bytes` |
| directionality | AUT-to-simulator, simulator-to-AUT, harness-to-AUT, or other |

If a driver does not support one of the common fault attrs, it must reject the
field explicitly rather than silently ignore it.

## 9. Driver capability declaration

The daemon must be able to discover driver capabilities.

Normative capability fields:

| Field | Type | Meaning |
|---|---|---|
| `type` | string | device type |
| `hotplug` | boolean | runtime create/destroy supported |
| `max_devices` | integer or `null` | implementation maximum, if bounded |
| `line_count` | integer or `null` | physical line count for line-oriented devices, else `null` |
| `persistent_attrs` | array of strings | writable attr names accepted by `device.set` |
| `injection_kinds` | array of strings | supported `fault.inject` kinds |
| `path_keys` | array of strings | path keys returned by `device.get` |

Illustrative capability object:

```json
{
  "type": "gpio",
  "hotplug": true,
  "max_devices": 32,
  "line_count": 8,
  "persistent_attrs": [
    "enabled",
    "latency_ns",
    "jitter_ns",
    "drop_rate_ppm",
    "bitflip_rate_ppm"
  ],
  "injection_kinds": ["line-write"],
  "path_keys": ["chip_path", "data_path", "sysfs_path"]
}
```

## 10. Driver-specific obligations

### 10.1 Stream-oriented drivers

Examples: `uart`, future `can`.

Additional obligations:

- define the AUT-facing endpoint path
- define the simulator-facing dataplane path if one exists
- define behavior on disconnect, reset, and destroy while endpoints are open

### 10.2 Line-oriented drivers

Examples: `gpio`.

Additional obligations:

- define line addressing and valid line ranges
- define behavior when the AUT currently owns a line as output
- define edge-event or sampled-value semantics where applicable
- define the simulator-facing bank-state dataplane semantics if a dataplane socket is exposed
- if a dataplane socket is exposed in `v0.2.0`, the device must expose exactly
  8 physical lines and advertise `line_count = 8`

## 11. Error behavior

Each driver contract must map failures into stable daemon-visible categories.

| Condition | Required outcome |
|---|---|
| unknown device name | `unknown-device` |
| unsupported attr name | `unknown-attribute` |
| attempt to write read-only field | `read-only-attribute` |
| invalid scalar or structured value | `invalid-value` or `invalid-payload` |
| state conflict | `state-conflict` |
| kernel-side failure | `kernel-failure` |

## 12. Rationale

**Why require hotplug for VirtRTLab-managed drivers?**  
The main `v0.2.0` goal is to let CI and operators reconfigure a lab without
reloading kernel modules or taking a privileged module-management path for every
test scenario.

**Why keep sysfs mandatory for observability?**  
It preserves shell-friendly diagnostics and keeps driver state inspectable even
when the daemon or CLI is not the active debugging tool.

**Why make writable sysfs secondary?**  
One canonical write-side control plane avoids semantic drift and permission
surprises between CLI, daemon, and direct shell access.

## 13. Decisions

- support for persistent fault attributes is sufficient for the base driver
  contract in `v0.2.0`; one-shot fault injection kinds are optional driver
  capabilities, not a compatibility gate
- all VirtRTLab-compatible drivers in `v0.2.0` attach to one shared
  kernel-visible VirtRTLab aggregation object for discovery and diagnostics;
  the observable hierarchy remains common even when internal implementation
  details differ by driver type