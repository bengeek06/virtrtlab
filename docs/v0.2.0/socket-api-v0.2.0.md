<!-- SPDX-License-Identifier: CC-BY-4.0 -->

# VirtRTLab Device Data Socket API (v0.2.0 Draft)

This document defines the `v0.2.0` device dataplane transport.

It replaces the `v0.1` assumption that the daemon socket surface is the only
socket interface in the system. In `v0.2.0`, per-device dataplane sockets and
the structured control plane are intentionally separated.

## 1. Overview

VirtRTLab uses two socket families in `v0.2.0`:

| Interface | Path | Payload | Consumer |
|---|---|---|---|
| Control socket | `/run/virtrtlab/control.sock` | JSON Lines | CLI, harnesses |
| Device data socket | `/run/virtrtlab/devices/<device>.sock` | device-specific raw dataplane | simulator process |

This document covers only the device dataplane sockets.

## 2. Common device dataplane rules

| Property | Value |
|---|---|
| Path pattern | resolved dataplane path per device; default installed pattern `/run/virtrtlab/devices/<device>.sock` |
| Address family | `AF_UNIX` |
| Socket type | `SOCK_STREAM` |
| Framing | device-specific, no control-plane framing |
| Direction | device-specific |

The AUT never opens this socket directly.

Common rules:

- each device that declares a dataplane exposes exactly one dataplane socket
- the simulator uses the dataplane socket without configuring the AUT-facing controller
- controller configuration remains the responsibility of the AUT through normal Linux interfaces such as `termios`, sysfs, or `ioctl`
- fault injection remains controlled through the control plane, not through the dataplane socket

## 3. Per-device mapping

| VirtRTLab device | Socket path |
|---|---|
| `uart0` | `/run/virtrtlab/devices/uart0.sock` |
| `gpio0` | `/run/virtrtlab/devices/gpio0.sock` |

There is no multiplexed global data socket in `v0.2.0`.

## 4. Connection semantics

| Rule | Required behavior |
|---|---|
| active connection count | one active simulator connection per dataplane socket |
| second connection while occupied | rejected |
| control separation | no control-plane request or response is carried on the dataplane socket |

## 5. UART dataplane

The UART dataplane socket carries a bidirectional raw byte stream.

| Property | Value |
|---|---|
| Socket path | `/run/virtrtlab/devices/uartN.sock` |
| Payload | raw bytes |
| Direction | bidirectional |
| Byte order | preserved end to end |
| Framing | none; no line or length framing |

The simulator does not configure baud rate, parity, stop bits, or other UART
controller properties through this socket. Those properties remain under AUT
control through `termios` on the AUT-facing TTY endpoint.

## 6. GPIO dataplane

The GPIO dataplane socket carries bank-state data for one GPIO device.

In the base `v0.2.0` contract, one data unit is one octet representing the
physical state of one 8-line GPIO bank.

| Property | Value |
|---|---|
| Socket path | `/run/virtrtlab/devices/gpioN.sock` |
| Payload width | 1 octet |
| Line mapping | bit `L` represents physical line `L` |
| Direction | bidirectional |

GPIO bank-state rules:

- bit `0` maps to line `0`
- bit `7` maps to line `7`
- only GPIO devices exposing exactly 8 physical lines are in scope for the
	dataplane socket contract in `v0.2.0`
- the octet represents the physical line state, not an AUT-specific logical view after `active_low`
- a simulator write updates the bank-state input side presented to the driver according to the GPIO driver contract
- one simulator write is one atomic bank-state update request for the full octet
- if any targeted line is currently owned by the AUT as output, the whole write is rejected with the driver-contract `state-conflict` outcome and no bit in the octet takes effect
- a simulator read returns the current physical bank state exported by the driver dataplane side

GPIO devices with a different physical line count may still be VirtRTLab
devices, but they are outside the `v0.2.0` GPIO dataplane socket contract
unless a later version defines a wider framing rule.

The simulator does not configure direction, edge detection, bias, `active_low`,
or line ownership through this socket. Those properties remain under AUT
control through the normal Linux GPIO userspace interfaces.

## 7. Reset and disconnect behavior

| Event | Required behavior |
|---|---|
| simulator disconnect | daemon closes the dataplane session for that device and returns to waiting state |
| bus or device reset invalidates the current data path | daemon closes and recreates the affected internal relay resources; the simulator connection is dropped |
| device destroy | existing sessions are closed, new connections fail because the endpoint no longer exists, and the data socket path disappears for that device |

Reconnect expectations after reset:

- after a reset-driven disconnect, the daemon returns to accepting a new dataplane connection for that device once the internal relay resources are usable again
- `v0.2.0` does not define a separate readiness notification on the dataplane socket itself
- simulator processes are expected to retry connection attempts according to the simulator contract rather than waiting for an out-of-band reconnect signal

Type-specific notes:

- for UART, disconnect handling may flush stale AUT-to-simulator bytes before returning to waiting state
- for GPIO, disconnect handling must not reconfigure the AUT-owned line parameters; only dataplane connectivity is reset

## 8. Fault-model interaction

The dataplane socket sees the device data after the active driver contract has
applied the configured transport semantics for that simulator-facing direction.

Rules:

- persistent fault attrs are controlled through the control socket
- one-shot fault injection is controlled through the control socket
- the dataplane socket itself carries only device dataplane payloads

## 9. Permissions

| Property | Required outcome |
|---|---|
| owner/group | group-visible to `virtrtlab` |
| mode | `0660` |
| intended effect | simulator processes run without root |

## 10. Rationale

**Why generalize to one dataplane socket per device?**  
It gives all simulator-facing devices a uniform integration model while keeping
AUT configuration responsibilities on the AUT side.

**Why keep controller configuration out of the dataplane?**  
The AUT must believe it is talking to a real controller. Baud rate, parity,
GPIO direction, edge settings, and similar controller configuration belong to
the AUT-facing Linux interfaces, not to the simulator-facing dataplane.

**Why version this document?**  
Because `v0.2.0` turns the dataplane into a first-class per-device concept and
extends it beyond UART.