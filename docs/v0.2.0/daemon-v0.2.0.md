<!-- SPDX-License-Identifier: CC-BY-4.0 -->

# virtrtlabd (v0.2.0 Draft)

This document defines the `v0.2.0` daemon contract.

In `v0.2.0`, `virtrtlabd` is no longer only a UART relay daemon. It becomes the
authoritative userspace control-plane process for VirtRTLab.

## 1. Responsibilities

The daemon is responsible for all of the following:

- exposing the canonical control socket
- materializing and destroying VirtRTLab device instances on demand
- managing the per-device dataplane sockets and the type-specific relay logic they require
- supervising simulator attachments and process lifecycle
- exporting lab status to CLI and CI clients

## 2. Runtime endpoints

| Endpoint | Path | Purpose |
|---|---|---|
| Control socket | resolved daemon control-socket path; default installed path `/run/virtrtlab/control.sock` | structured control-plane requests and responses |
| Device data socket | resolved per-device dataplane path; default installed pattern `/run/virtrtlab/devices/<device>.sock` | simulator dataplane for one device |
| Runtime root | resolved daemon runtime root; default installed path `/run/virtrtlab/` | daemon-owned state, pid files, sockets, simulator runtime metadata |

The device dataplane contract remains separately specified in
[socket-api-v0.2.0.md](socket-api-v0.2.0.md).

The control protocol is specified in
[control-socket-v0.2.0.md](control-socket-v0.2.0.md).

The daemon configuration contract is specified in
[daemon-config-v0.2.0.md](daemon-config-v0.2.0.md).

## 3. Lifecycle

### 3.1 Daemon process lifecycle

| Phase | Required behavior |
|---|---|
| startup | load configuration, create or validate the configured runtime root, bind the resolved control socket, load driver capability inventory, and begin serving requests |
| steady state | accept concurrent control clients, maintain current topology, drive per-device dataplanes, monitor simulator processes |
| shutdown | stop managed simulators, close sockets, and destroy only the runtime state owned by the active daemon instance |

### 3.2 Lab lifecycle

The daemon maintains one lab state machine.

| State | Meaning |
|---|---|
| `empty` | no VirtRTLab device instances are materialized |
| `configuring` | topology change is in progress |
| `up` | one or more devices exist and the control plane is operational |
| `tearing-down` | destruction is in progress |
| `error` | the daemon detected an operational failure that requires operator attention |

## 4. Concurrency rules

| Rule | Required behavior |
|---|---|
| topology mutation serialization | only one topology-changing operation (`lab.up`, `lab.down`, `device.create`, `device.destroy`, `lab.apply_profile`) may commit at a time |
| read operations | multiple read-only requests may run concurrently |
| fault injection | may run concurrently with read operations and active AUT traffic for the target device |
| simulator lifecycle | per-device simulator state transitions are serialized per device |

## 5. Data plane separation

The daemon must preserve a strict separation between:

| Plane | Interface | Payload |
|---|---|---|
| control plane | resolved daemon control-socket path; default installed path `/run/virtrtlab/control.sock` | JSON requests/responses/events |
| device data plane | resolved per-device dataplane path; default installed pattern `/run/virtrtlab/devices/<device>.sock` | device-specific dataplane payload |

The daemon must not multiplex dataplane payloads inside the control socket.

## 6. Status model

The daemon exposes status through `lab.status` and related control actions.

Illustrative result:

```json
{
  "daemon": {
    "state": "up",
    "pid": 2841,
    "control_socket": "/run/virtrtlab/control.sock",
    "control_api_version": "0.2.0"
  },
  "devices": [
    {"name": "uart0", "type": "uart"},
    {"name": "gpio0", "type": "gpio"}
  ],
  "simulators": [
    {"device": "uart0", "simulator": "loopback", "state": "running"}
  ]
}
```

## 7. Logging

The daemon may log to stderr, syslog, or journald according to deployment.

The only normative logging requirement in `v0.2.0` is that operator-visible
control-plane failures remain queryable through control responses and simulator
state inspection. Log formatting itself is not the external contract.

## 8. Failure behavior

| Failure class | Required observable behavior |
|---|---|
| malformed client request | reject that request with `invalid-request`; keep the connection open unless framing is unrecoverable |
| topology change failure | report structured error; keep previous stable topology visible |
| simulator crash | update simulator state to `failed`; if subscribed clients exist, emit a `simulator-state-changed` event |
| control socket bind failure | daemon startup fails |
| device dataplane socket bind or driver-side open failure for one device | corresponding create or up operation fails for that device; topology and partial auto-start outcomes follow the control-socket contract for `device.create`, `lab.up`, and `lab.apply_profile` |

## 9. Service-manager integration

The daemon contract assumes a service manager or equivalent launch mechanism is
used in normal installations.

Required outcomes:

- the daemon starts automatically or on operator demand without interactive `sudo`
- the daemon keeps a stable control socket path across restarts
- runtime directory ownership and modes satisfy
  [privilege-model-v0.2.0.md](privilege-model-v0.2.0.md)

## 10. Rationale

**Why make the daemon authoritative?**  
It gives VirtRTLab one state coordinator for dynamic topology, simulator
attachment, and fault control, which is necessary once devices become hotpluggable.

**Why keep a simulator-facing dataplane?**  
The existing UART byte-stream design is still useful, and the same separation
now extends naturally to other device classes such as GPIO. `v0.2.0` extends
the daemon instead of replacing the transport model.

## 11. Decisions

- `lab.down` destroys all active device instances and stops managed simulators,
  but does not stop the daemon process itself
- aggregate and per-device simulator runtime state files under the configured
  simulator runtime root remain part of the normative simulator-observability
  contract in `v0.2.0`; they do not replace the control socket as the topology
  control API