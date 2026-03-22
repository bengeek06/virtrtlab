<!-- SPDX-License-Identifier: CC-BY-4.0 -->

# VirtRTLab Control Socket (v0.2.0 Draft)

This document is the source of truth for the `v0.2.0` control plane.

It defines the daemon-mediated interface used by `virtrtlabctl` and other
authorized clients to manage lab topology, device configuration, fault
injection, and simulator lifecycle without requiring full root access for
routine operations.

The UART byte-stream transport remains specified separately in
[socket-api-v0.2.0.md](socket-api-v0.2.0.md).

## 1. Scope

The control socket contract covers:

- daemon discovery and access rules
- request/response framing
- command families and payload shapes
- error reporting
- observability and event streaming

The control socket contract does not cover:

- the device dataplane stream exchanged with simulators
- kernel-internal APIs used by drivers to create or destroy devices
- the on-disk format of non-normative daemon debug logs

## 2. Overview

In `v0.2.0`, VirtRTLab exposes one canonical control endpoint:

| Property | Value |
|---|---|
| Path | resolved daemon control-socket path; default installed path `/run/virtrtlab/control.sock` |
| Address family | `AF_UNIX` |
| Socket type | `SOCK_STREAM` |
| Framing | UTF-8 JSON Lines (`\n`-terminated objects) |
| Access model | daemon-owned socket, group-accessible to `virtrtlab` |

Each request is one JSON object on one line. Each response is one JSON object on
one line.

Requests and responses are correlated by a client-supplied `id` field.

Unless a surrounding rule says otherwise, JSON objects shown in this document
are illustrative examples. Field presence, requiredness, and error behavior are
defined by the accompanying tables and rules.

## 3. Connection model

### 3.1 Client classes

| Client | Typical use | Allowed operations |
|---|---|---|
| `virtrtlabctl` | normal operator and CI control | all documented command families |
| harness script | fault injection, status polling | `lab.*`, `device.*`, `fault.*`, `sim.*`, and `events.subscribe` when supported |
| diagnostic tool | read-only inspection | `lab.status`, `device.list`, `device.get`, `device.stats` |

### 3.2 Session rules

| Rule | Required behavior |
|---|---|
| Encoding | all frames are UTF-8 JSON text ending with `\n` |
| Pipelining | allowed; multiple requests may be outstanding on one connection |
| Ordering | responses may arrive out of submission order; clients must match by `id` |
| Authentication | based on filesystem permissions of the Unix socket; no in-band login |
| Backward compatibility | unknown top-level fields must be ignored unless explicitly required by the selected action |

## 4. Base envelope

### 4.1 Request envelope

Every request object uses this base shape:

```json
{
  "id": "req-0001",
  "action": "device.set",
  "params": {
    "device": "uart0",
    "values": {
      "latency_ns": 500000
    }
  }
}
```

| Field | Type | Required | Meaning |
|---|---|---|---|
| `id` | string | yes | opaque request identifier chosen by the client |
| `action` | string | yes | operation name such as `lab.up` or `fault.inject` |
| `params` | object | yes | action-specific payload |

### 4.2 Success response envelope

```json
{
  "id": "req-0001",
  "ok": true,
  "result": {
    "device": "uart0",
    "applied": {
      "latency_ns": 500000
    }
  }
}
```

| Field | Type | Required | Meaning |
|---|---|---|---|
| `id` | string | yes | echoed request identifier |
| `ok` | boolean | yes | `true` on success |
| `result` | object | yes | action-specific result payload |

### 4.3 Error response envelope

```json
{
  "id": "req-0002",
  "ok": false,
  "error": {
    "code": "unknown-device",
    "message": "unknown device: uart9",
    "retryable": false,
    "details": {
      "device": "uart9"
    }
  }
}
```

| Field | Type | Required | Meaning |
|---|---|---|---|
| `id` | string | yes | echoed request identifier |
| `ok` | boolean | yes | `false` on failure |
| `error.code` | string | yes | stable machine-readable error code |
| `error.message` | string | yes | short operator-facing message |
| `error.retryable` | boolean | yes | whether retry without user correction may succeed |
| `error.details` | object | no | action-specific structured details |

## 5. Command families

### 5.1 `lab.*`

| Action | Purpose |
|---|---|
| `lab.up` | materialize a lab topology from inline counts or a resolved profile |
| `lab.down` | stop simulators, destroy devices, and return the lab to empty state |
| `lab.status` | return daemon status, topology summary, and compatibility info |
| `lab.apply_profile` | replace the current topology and simulator attachment set using one profile payload |

#### `lab.up`

Request example:

```json
{
  "id": "req-up-1",
  "action": "lab.up",
  "params": {
    "devices": [
      {"type": "uart", "count": 2},
      {"type": "gpio", "count": 1}
    ],
    "attachments": [
      {
        "device": "uart0",
        "simulator": "loopback",
        "auto_start": true,
        "config": {"delay_ms": 0}
      }
    ]
  }
}
```

Success result:

```json
{
  "id": "req-up-1",
  "ok": true,
  "result": {
    "state": "up",
    "devices": [
      {
        "name": "uart0",
        "type": "uart",
        "paths": {
          "aut_path": "/dev/ttyVIRTLAB0",
          "data_path": "/run/virtrtlab/devices/uart0.sock",
          "sysfs_path": "/sys/kernel/virtrtlab/devices/uart0"
        }
      },
      {
        "name": "uart1",
        "type": "uart",
        "paths": {
          "aut_path": "/dev/ttyVIRTLAB1",
          "data_path": "/run/virtrtlab/devices/uart1.sock",
          "sysfs_path": "/sys/kernel/virtrtlab/devices/uart1"
        }
      },
      {
        "name": "gpio0",
        "type": "gpio",
        "paths": {
          "chip_path": "/dev/gpiochip4",
          "data_path": "/run/virtrtlab/devices/gpio0.sock",
          "sysfs_path": "/sys/kernel/virtrtlab/devices/gpio0"
        }
      }
    ],
    "attachments": [
      {
        "device": "uart0",
        "simulator": "loopback",
        "state": "running"
      }
    ]
  }
}
```

Behavior:

- validates the full requested topology before partial creation
- reconciles the active lab toward the requested set from any stable lab state
- creates and destroys devices to match the requested set
- applies declared attachments after the requested topology exists
- starts only attachments whose effective `auto_start` is `true`

Mutation phases:

| Phase | Atomicity rule |
|---|---|
| topology and attachment-definition validation | all-or-nothing; any validation error prevents mutation |
| device inventory and attachment-definition reconciliation | all-or-nothing; if this phase fails, the previously stable topology and attachment definitions remain visible |
| simulator auto-start for attachments whose effective `auto_start` is `true` | not rolled back automatically; a failure returns structured partial-state details |

Validation rules:

- `devices` is required for `lab.up`
- duplicate `type` entries in one request are invalid
- `count` must be a positive integer and must not exceed the advertised driver capability limit
- attachment validation is atomic with topology validation; an invalid attachment prevents any topology mutation

Failure rules:

- if device create, device destroy, or attachment-definition materialization fails after successful validation, `lab.up` returns failure and the previously stable topology remains visible
- if simulator auto-start is only partially successful after successful reconciliation, `lab.up` returns failure with partial state details; the reconciled topology and attachment definitions remain in effect
- clients must treat `lab.up` as a target-state reconcile operation, not as an `empty -> up` bootstrap only

#### `lab.down` result

Illustrative success result:

```json
{
  "id": "req-down-1",
  "ok": true,
  "result": {
    "state": "empty",
    "stopped_simulators": ["uart0"],
    "destroyed_devices": ["uart0", "uart1", "gpio0"]
  }
}
```

#### `lab.down`

Behavior:

- stops all managed simulators first
- destroys all VirtRTLab device instances
- leaves the daemon process itself running unless the service manager stops it separately

#### `lab.status`

Request example:

```json
{
  "id": "req-status-1",
  "action": "lab.status",
  "params": {}
}
```

Success result fields:

| Field | Type | Meaning |
|---|---|---|
| `daemon.state` | string | daemon lab state such as `empty`, `up`, or `error` |
| `daemon.pid` | integer | daemon process id |
| `daemon.control_socket` | string | resolved control socket path |
| `daemon.control_api_version` | string | control API version string |
| `daemon.control_api_framing` | string | control API framing identifier, initial value `jsonl` |
| `devices` | array | current device summaries |
| `simulators` | array | current simulator attachment summaries |
| `capabilities` | array | driver capability summaries known to the daemon |

`lab.status` is read-only and must not mutate topology or simulator state.

#### `lab.apply_profile`

Request example:

```json
{
  "id": "req-apply-1",
  "action": "lab.apply_profile",
  "params": {
    "profile": {
      "devices": [
        {"type": "uart", "count": 1},
        {"type": "gpio", "count": 1}
      ],
      "attachments": [
        {
          "device": "uart0",
          "simulator": "loopback",
          "auto_start": true,
          "config": {"delay_ms": 0}
        }
      ]
    }
  }
}
```

Rules:

- `lab.apply_profile` replaces the active lab topology with the resolved target profile
- profile validation is atomic
- if simulator auto-start is only partially successful after validation, the command returns failure with partial state details, matching the CLI contract
- topology and attachment-definition reconciliation follow the same all-or-nothing rules as `lab.up`

### 5.2 `device.*`

| Action | Purpose |
|---|---|
| `device.list` | enumerate current devices and their resolved host paths |
| `device.create` | add one device instance dynamically |
| `device.destroy` | remove one device instance dynamically |
| `device.get` | read device attributes, identity, and resolved paths |
| `device.set` | update writable device attributes |
| `device.reset` | apply the device reset contract |
| `device.stats` | read per-device stats |
| `device.stats_reset` | reset per-device stats |

#### `device.create`

```json
{
  "id": "req-create-1",
  "action": "device.create",
  "params": {
    "type": "uart",
    "index": 2
  }
}
```

Rules:

- `index` is optional; if omitted, the daemon allocates the lowest free index for that device type
- creation must fail with `already-exists` if the requested `type + index` tuple already exists
- creation must fail with `unsupported-device-type` for unknown or unloaded driver classes

Illustrative success result:

```json
{
  "id": "req-create-1",
  "ok": true,
  "result": {
    "name": "uart2",
    "type": "uart",
    "paths": {
      "aut_path": "/dev/ttyVIRTLAB2",
      "data_path": "/run/virtrtlab/devices/uart2.sock",
      "sysfs_path": "/sys/kernel/virtrtlab/devices/uart2"
    }
  }
}
```

#### `device.destroy`

Request example:

```json
{
  "id": "req-destroy-1",
  "action": "device.destroy",
  "params": {
    "device": "uart2"
  }
}
```

Rules:

- if the target device has an attached simulator, the daemon must stop or detach it before destruction completes
- destroying an unknown device returns `unknown-device`
- successful destroy removes the device from subsequent `device.list` results immediately

#### `device.list`

Request example:

```json
{
  "id": "req-list-1",
  "action": "device.list",
  "params": {}
}
```

Success result shape:

```json
{
  "id": "req-list-1",
  "ok": true,
  "result": {
    "devices": [
      {
        "name": "gpio0",
        "type": "gpio",
        "state": "up",
        "paths": {
          "chip_path": "/dev/gpiochip4",
          "data_path": "/run/virtrtlab/devices/gpio0.sock",
          "sysfs_path": "/sys/kernel/virtrtlab/devices/gpio0"
        }
      }
    ]
  }
}
```

#### `device.get`

Request example:

```json
{
  "id": "req-get-1",
  "action": "device.get",
  "params": {
    "device": "gpio0"
  }
}
```

Success result fields:

| Field | Type | Meaning |
|---|---|---|
| `name` | string | device name |
| `type` | string | device type |
| `state` | string | current device lifecycle state; one of `creating`, `up`, `resetting`, `destroying`, or `error` |
| `paths` | object | resolved AUT, dataplane, and sysfs paths |
| `attrs` | object | current control-plane-visible attributes |
| `stats` | object | current per-device stats counters |

#### `device.set`

```json
{
  "id": "req-set-1",
  "action": "device.set",
  "params": {
    "device": "gpio0",
    "values": {
      "enabled": true,
      "latency_ns": 100000,
      "drop_rate_ppm": 1000
    }
  }
}
```

Rules:

- all field validation is atomic per request: if one value is invalid, none are applied
- unknown writable keys return `unknown-attribute`
- read-only keys return `read-only-attribute`
- common persistent-attribute validation follows the driver-contract ranges: `enabled` is boolean, `latency_ns` and `jitter_ns` are non-negative nanosecond integers subject to implementation maxima, and `drop_rate_ppm` plus `bitflip_rate_ppm` are integers in the inclusive range `0..1000000`

Success result:

- returns the applied attribute set and the resulting current values for those keys

#### `device.reset`

Request example:

```json
{
  "id": "req-reset-1",
  "action": "device.reset",
  "params": {
    "device": "uart0"
  }
}
```

Rules:

- applies the type-specific reset baseline defined by the driver contract
- does not rename the device or change its index
- returns the resulting current attrs and stats snapshot after reset

#### `device.stats`

Request example:

```json
{
  "id": "req-stats-1",
  "action": "device.stats",
  "params": {
    "device": "uart0"
  }
}
```

Success result:

- returns one `stats` object for the target device
- counter names and units are type-specific and must match the driver contract

#### `device.stats_reset`

Request example:

```json
{
  "id": "req-stats-reset-1",
  "action": "device.stats_reset",
  "params": {
    "device": "uart0"
  }
}
```

Rules:

- resets only the target device counters
- returns the post-reset `stats` object
- does not modify non-stat attributes

### 5.3 `fault.*`

| Action | Purpose |
|---|---|
| `fault.inject` | inject an immediate one-shot event on a device |
| `fault.profile_apply` | apply a named or inline fault profile to a device |
| `fault.profile_clear` | remove an applied named profile from a device |

#### `fault.inject`

GPIO example:

```json
{
  "id": "req-inject-1",
  "action": "fault.inject",
  "params": {
    "device": "gpio0",
    "kind": "line-write",
    "payload": {
      "line": 0,
      "value": 1
    }
  }
}
```

UART example:

```json
{
  "id": "req-inject-2",
  "action": "fault.inject",
  "params": {
    "device": "uart0",
    "kind": "rx-bytes",
    "payload": {
      "encoding": "base64",
      "data": "AQIDBA=="
    }
  }
}
```

Rules:

- supported `kind` values are device-type specific and defined by the driver contract
- payload validation is strict; malformed or unsupported fields return `invalid-payload`
- one-shot injection does not implicitly modify persistent fault attributes unless the selected injection kind says so explicitly

#### `fault.profile_apply`

`fault.profile_apply` is part of the `v0.2.0` minimum control-plane contract.

Named-profile example:

```json
{
  "id": "req-profile-1",
  "action": "fault.profile_apply",
  "params": {
    "device": "uart0",
    "profile": {
      "name": "slow-noisy-uart"
    }
  }
}
```

Inline-profile example:

```json
{
  "id": "req-profile-2",
  "action": "fault.profile_apply",
  "params": {
    "device": "gpio0",
    "profile": {
      "inline": {
        "enabled": true,
        "latency_ns": 100000,
        "jitter_ns": 10000,
        "drop_rate_ppm": 1000,
        "bitflip_rate_ppm": 0
      }
    }
  }
}
```

Rules:

- exactly one of `profile.name` or `profile.inline` must be present
- inline profile validation follows the same type and range rules as `device.set`
- named profile resolution is daemon-defined but must be deterministic for one daemon configuration
- a successful apply returns the effective persistent fault state visible on the target device

#### `fault.profile_clear`

```json
{
  "id": "req-profile-clear-1",
  "action": "fault.profile_clear",
  "params": {
    "device": "uart0"
  }
}
```

Rules:

- clears the currently applied named or inline profile from the target device
- returns the resulting persistent fault state after clear

Result fields for both `fault.profile_apply` and `fault.profile_clear`:

| Field | Type | Meaning |
|---|---|---|
| `device` | string | target device name |
| `profile` | object or `null` | effective applied profile metadata, or `null` after clear |
| `effective_attrs` | object | resulting persistent fault attrs now active on the device |

### 5.4 `sim.*`

| Action | Purpose |
|---|---|
| `sim.list` | list visible simulator catalog entries |
| `sim.inspect` | inspect one simulator catalog entry |
| `sim.attach` | attach a simulator definition to one device |
| `sim.detach` | detach the simulator from one device |
| `sim.start` | start the attached simulator process |
| `sim.stop` | stop the attached simulator process |
| `sim.status` | inspect aggregate or per-device simulator lifecycle state |

`sim.*` behavior is normatively aligned with
[simulator-contract-v0.2.0.md](simulator-contract-v0.2.0.md) and
[virtrtlabctl-v0.2.0.md](virtrtlabctl-v0.2.0.md).

#### `sim.list`

Request example:

```json
{
  "id": "req-sim-list-1",
  "action": "sim.list",
  "params": {
    "type": "uart",
    "verbose": false
  }
}
```

Rules:

- `type` is optional and filters visible catalog entries by supported device type
- `verbose` is optional and defaults to `false`
- `sim.list` is read-only and must not mutate runtime state

Success result minimum fields:

| Field | Type | Meaning |
|---|---|---|
| `simulators` | array | visible catalog entries after precedence resolution |

Each simulator summary contains at least `name`, `version`, `supports`, and
`summary`. When `verbose = true`, each summary also contains `catalog_file` and
`overrides`.

#### `sim.inspect`

Request example:

```json
{
  "id": "req-sim-inspect-1",
  "action": "sim.inspect",
  "params": {
    "name": "loopback",
    "version": "1.0.0"
  }
}
```

Rules:

- `name` is required
- `version` is optional only when exactly one visible catalog entry exists for that simulator name
- ambiguity without `version` returns `ambiguous-simulator-version` with structured details

Success result minimum fields:

| Field | Type | Meaning |
|---|---|---|
| `name` | string | simulator name |
| `version` | string | selected simulator version |
| `supports` | array | supported device kinds |
| `summary` | string | one-line description |
| `catalog_file` | string | resolved catalog entry path |
| `restart_policy` | string | declared restart policy |

If declared, `description` and `parameters` are included with the same meaning
as in the simulator contract.

#### `sim.attach`

Request example:

```json
{
  "id": "req-sim-attach-1",
  "action": "sim.attach",
  "params": {
    "device": "uart0",
    "simulator": "loopback",
    "version": "1.0.0",
    "auto_start": false,
    "config": {
      "delay_ms": 5
    }
  }
}
```

Rules:

- the target device must already exist
- the selected simulator must support the target device type
- if multiple visible versions share the selected simulator name and `version` is omitted, the request returns `ambiguous-simulator-version`
- a second `sim.attach` on the same device replaces the existing attachment only when the current attachment state is `attached`, `stopped`, or `failed`
- replacement while the current attachment is `starting`, `running`, or `stopping` returns `state-conflict`
- successful attach creates or refreshes the daemon-owned runtime attachment state for that device

Success result minimum fields:

| Field | Type | Meaning |
|---|---|---|
| `device` | string | target device |
| `simulator` | string | attached simulator name |
| `version` | string | selected simulator version |
| `state` | string | resulting attachment lifecycle state, initially `attached` |
| `auto_start` | boolean | effective attachment auto-start policy |

#### `sim.detach`

Request example:

```json
{
  "id": "req-sim-detach-1",
  "action": "sim.detach",
  "params": {
    "device": "uart0"
  }
}
```

Rules:

- if the attachment is running, the daemon stops it first
- successful detach removes the per-device runtime attachment directory
- detaching a non-attached device returns `not-attached`; an absent device returns `unknown-device`

Success result minimum fields:

| Field | Type | Meaning |
|---|---|---|
| `device` | string | target device |
| `state` | string | resulting state, always `detached` |

#### `sim.start`

Request example:

```json
{
  "id": "req-sim-start-1",
  "action": "sim.start",
  "params": {
    "device": "uart0"
  }
}
```

Rules:

- the target device must exist and have an attached simulator
- `sim.start` on a `running` or `starting` attachment returns `state-conflict`
- successful start returns the attachment state after immediate launch checks
- no simulator-specific readiness probe is required by the `v0.2.0` base contract

Success result minimum fields:

| Field | Type | Meaning |
|---|---|---|
| `device` | string | target device |
| `simulator` | string | running simulator name |
| `version` | string | selected simulator version |
| `state` | string | resulting lifecycle state |
| `pid` | integer or `null` | simulator process id when available |

#### `sim.stop`

Request example:

```json
{
  "id": "req-sim-stop-1",
  "action": "sim.stop",
  "params": {
    "device": "uart0"
  }
}
```

Rules:

- the target device must exist and have an attachment
- if the attachment state is already `stopped`, the daemon returns success with the unchanged `stopped` snapshot
- `sim.stop` on an `attached` or `failed` attachment returns `state-conflict`
- graceful termination is attempted first
- if the process does not exit within the bounded timeout, the daemon may force termination and still return success if the process is observed dead afterward

Success result minimum fields:

| Field | Type | Meaning |
|---|---|---|
| `device` | string | target device |
| `state` | string | resulting lifecycle state, normally `stopped` |
| `last_exit_code` | integer or `null` | latest observed terminal exit code |

#### `sim.status`

Request example without device:

```json
{
  "id": "req-sim-status-1",
  "action": "sim.status",
  "params": {}
}
```

Request example with device:

```json
{
  "id": "req-sim-status-2",
  "action": "sim.status",
  "params": {
    "device": "uart0"
  }
}
```

Rules:

- without `device`, returns aggregate attachment summaries
- with `device`, returns one coherent per-device snapshot
- if the device exists but no attachment exists, returns `not-attached` with details identifying the unattached device

Success result minimum fields for aggregate status:

| Field | Type | Meaning |
|---|---|---|
| `attachments` | array | zero or more attachment summaries |

Each attachment summary contains at least `device`, `state`, `simulator`,
`simulator_version`, `pid`, `auto_start`, and `updated_at`.

Success result minimum fields for per-device status:

| Field | Type | Meaning |
|---|---|---|
| `device` | string | target device |
| `device_type` | string | device kind |
| `simulator` | string | simulator name |
| `simulator_version` | string | selected simulator version |
| `state` | string | attachment lifecycle state |
| `auto_start` | boolean | effective auto-start policy |
| `restart_policy` | string | effective restart policy |
| `pid` | integer or `null` | simulator process id when alive |

#### `sim.logs`

`sim.logs` is not part of the control-socket request/response contract in `v0.2.0`.

Rules:

- log streaming remains a CLI-level operation over daemon-owned runtime log files
- the control socket may expose metadata needed by `sim.status`, but it does not need to proxy log lines in `v0.2.0`

### 5.5 `events.subscribe`

`events.subscribe` is optional in `v0.2.0`.

The daemon may support event streaming on the same connection.

Subscription request:

```json
{
  "id": "req-sub-1",
  "action": "events.subscribe",
  "params": {
    "topics": ["lab", "device", "sim"]
  }
}
```

After the normal success response, the daemon may emit unsolicited event objects
on the same connection.

Event example:

```json
{
  "event": "device-added",
  "topic": "device",
  "sequence": 17,
  "payload": {
    "device": "uart2",
    "type": "uart"
  }
}
```

Event rules:

- event objects do not contain `id`
- `sequence` is monotonically increasing per daemon instance
- event delivery is best-effort; clients must resynchronize with explicit read commands after reconnect
- clients must not depend on `events.subscribe` as the only way to observe state changes

## 6. Error codes

The following `error.code` values are stable in `v0.2.0`:

| Code | Meaning |
|---|---|
| `invalid-request` | malformed JSON, missing required fields, or wrong top-level types |
| `unknown-action` | unsupported `action` name |
| `permission-denied` | caller lacks permission for the operation |
| `unknown-device` | referenced device does not exist |
| `unknown-simulator` | referenced simulator does not exist |
| `unsupported-device-type` | no driver contract exists for the requested device type |
| `already-exists` | requested object already exists |
| `state-conflict` | operation is invalid in the current lifecycle state |
| `unknown-attribute` | write attempted on an unknown key |
| `read-only-attribute` | write attempted on a read-only key |
| `invalid-value` | attribute value failed range or type validation |
| `invalid-payload` | injection or structured request payload failed validation |
| `incompatible-target` | referenced objects exist but cannot be combined for the requested operation |
| `kernel-failure` | daemon could not complete the requested kernel-side operation |
| `simulator-failure` | simulator process launch or stop failed |
| `not-attached` | simulator lifecycle action referenced an existing device without an attachment |
| `ambiguous-simulator-version` | simulator name matched multiple visible versions without explicit selection |

Recommended action-specific error mapping:

| Action | Condition | Error code |
|---|---|---|
| `lab.up` | duplicate device type entry in one request | `invalid-request` |
| `lab.up` | requested count exceeds driver capability | `invalid-value` |
| `device.create` | requested `type + index` already exists | `already-exists` |
| `device.destroy` | target absent | `unknown-device` |
| `device.get` | target absent | `unknown-device` |
| `device.set` | one or more invalid values | `invalid-value` |
| `device.stats_reset` | target absent | `unknown-device` |
| `fault.inject` | unknown injection kind for device type | `invalid-payload` |
| `sim.inspect` | ambiguous simulator version | `ambiguous-simulator-version` |
| `sim.attach` | ambiguous simulator version | `ambiguous-simulator-version` |
| `sim.attach` | simulator unsupported for device type | `incompatible-target` |
| `sim.detach` | device has no attachment | `not-attached` |
| `sim.start` | device has no attachment | `not-attached` |
| `sim.start` | attachment already `running` or `starting` | `state-conflict` |
| `sim.stop` | attachment absent | `not-attached` |
| `sim.stop` | attachment exists but is not live and not `stopped` | `state-conflict` |
| `sim.status` with `device` | device exists but attachment absent | `not-attached` |

## 7. Versioning

The daemon exposes its control API version through `lab.status`.

Illustrative `lab.status` daemon fragment:

```json
{
  "daemon": {
    "control_socket": "/run/virtrtlab/control.sock",
    "control_api_version": "0.2.0",
    "control_api_framing": "jsonl"
  }
}
```

Compatibility rules:

- `v0.2.x` clients must ignore unknown result fields
- breaking control-plane changes require a new versioned specification document

## 8. Rationale

**Why one canonical control socket?**  
It centralizes authorization, state validation, and lifecycle sequencing in one
daemon instead of spreading control across `sysfs`, `sudo`, and ad hoc shell
wrappers.

**Why JSON Lines over a Unix stream socket?**  
It keeps the protocol easy to debug with ordinary tooling while still supporting
long-lived connections and future event streaming.

**Why keep data and control separate?**  
The device dataplane stays tool-friendly and device-oriented, while
the control plane gains structured validation and explicit errors.

## 9. Decisions

- `fault.profile_apply` and `fault.profile_clear` are part of the `v0.2.0`
  minimum control-plane contract
- `events.subscribe` remains optional in `v0.2.0`; polling through read-side
  actions such as `lab.status`, `device.get`, and `sim.status` remains the
  required interoperability path