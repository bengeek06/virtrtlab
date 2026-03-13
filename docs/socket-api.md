# VirtRTLab — Wire device and daemon socket (v1)

Source of truth is the root [README.md](../README.md). This file keeps transport details focused.

## Overview

VirtRTLab uses two transport primitives:

| Transport | Path | Direction | Protocol |
|---|---|---|---|
| Wire misc device | `/dev/virtrtlab-wireN` | kernel ↔ daemon | raw bytes |
| Daemon socket | `/run/virtrtlab/uart0.sock` | daemon ↔ simulator | raw bytes |

Control operations (fault injection, configuration) are done via **sysfs** — not via the socket.

## Wire device (`/dev/virtrtlab-wireN`)

One per UART instance (N = 0, 1, …).

- Opened exclusively by `virtrtlabd`: `open()` returns `-EBUSY` if the device is already open (enforced by an `atomic_t open_count` checked at `open()` time)
- `read()` — bytes coming from the AUT (via `/dev/ttyVIRTLABx`)
- `write()` — bytes going toward the AUT
- `poll()`/`select()` — both `POLLIN` and `POLLOUT` supported

The kernel applies fault injection (latency, jitter, drop, bitflip) **before** delivering bytes to the wire device.

## Daemon socket (`/run/virtrtlab/uart0.sock`)

- Type: `AF_UNIX`, `SOCK_STREAM`
- Created by `virtrtlabd` at startup; removed on clean shutdown
- One socket per device: `uart0.sock`, `uart1.sock`, …
- The simulator connects and exchanges raw bytes — no framing, no length prefix

Bytes are delivered in-order, preserving the UART byte stream.

## Relay logic (virtrtlabd)

```python
while select([wire_fd, sock_fd]):
    if wire_fd readable:
        data = read(wire_fd)      # from AUT
        write(sock_fd, data)      # to simulator
    if sock_fd readable:
        data = read(sock_fd)      # from simulator
        write(wire_fd, data)      # to AUT
```

## Testing

```sh
# Connect a terminal to the simulated UART
socat - UNIX-CONNECT:/run/virtrtlab/uart0.sock

# Wire two UART instances together for loopback testing
socat UNIX-CONNECT:/run/virtrtlab/uart0.sock \\
      UNIX-CONNECT:/run/virtrtlab/uart1.sock

# Inspect the stream
socat - UNIX-CONNECT:/run/virtrtlab/uart0.sock | xxd

# Replay a capture
cat capture.bin | socat - UNIX-CONNECT:/run/virtrtlab/uart0.sock
```

## Rationale

**Why raw bytes instead of JSONL?**  
The AUT exchanges raw bytes with the simulated UART. Wrapping bytes in JSONL would require an extra encode/decode step in the daemon and add latency. Fault injection (drops, bitflips) operates at the byte level in the kernel — the userspace socket sees the already-mutated stream.

**Why per-device sockets instead of a multiplexed global socket?**  
Per-device sockets let the simulator process use a plain `connect()` to `/run/virtrtlab/uart0.sock` with no demultiplexing logic. This also matches what `socat` and raw POSIX tools expect.

## Open questions

> **Open:** Should the daemon support multiple simultaneous connections per socket (e.g. one observer + one active simulator)? The current design assumes one active connection per socket.

> **Open:** Should `virtrtlabd` automatically re-create the socket if the simulator disconnects and reconnects, without restarting the daemon?
