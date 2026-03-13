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
- `read()` — bytes coming from the AUT (via `/dev/ttyVIRTLABx`); blocks until data is available
- `write()` — bytes going toward the AUT; **non-blocking**: returns `-EAGAIN` immediately if the RX buffer is full (never blocks the relay loop)
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
# Simplified relay loop in virtrtlabd

def open_wire():
    return open('/dev/virtrtlab-wire0', O_RDWR)  # exclusive; a concurrent second open() returns -EBUSY

server_sock = socket(AF_UNIX, SOCK_STREAM)
bind(server_sock, '/run/virtrtlab/uart0.sock')
listen(server_sock)
wire_fd = open_wire()

while True:
    # Wait for a simulator to connect
    client_sock = accept(server_sock)

    # Relay until simulator disconnects or bus transitions
    while True:
        r, _, _ = select([wire_fd, client_sock], [], [])
        if wire_fd in r:
            data, err = read(wire_fd)          # from AUT
            if err == EIO:                     # state=down: bus halted, wire fd still valid
                time.sleep(0.01)               # back-off; POLLHUP|POLLERR clears on state=up
                continue
            if not data:                       # EOF: state=reset has invalidated this wire_fd
                close(wire_fd)
                wire_fd = open_wire()          # re-open for clean post-reset state
                break                          # disconnect simulator; next accept() is clean
            write(client_sock, data)           # to simulator
        if client_sock in r:
            data = recv(client_sock)
            if not data:                       # simulator disconnected
                # Drain pending AUT→daemon bytes so next simulator starts clean.
                # wire_fd stays open to receive bus state notifications.
                set_nonblocking(wire_fd)
                while True:
                    stale, err = read(wire_fd)
                    if err == EAGAIN:          # all pending AUT→daemon bytes drained
                        break
                    if err == EIO:             # bus transitioned to state=down during drain
                        break                  # keep wire_fd open; outer loop observes state changes
                    if not stale:              # EOF: state=reset invalidated wire_fd
                        close(wire_fd)
                        wire_fd = open_wire()
                        break
                set_blocking(wire_fd)
                break
            write(wire_fd, data)               # to AUT (non-blocking: EAGAIN → overrun)
```

On simulator disconnect, the daemon drains only the AUT→daemon direction to avoid replaying stale bytes into the next simulator session. Drain runs in non-blocking mode until `read()` returns `-EAGAIN`; if the bus transitions to `state=down`, `read()` may return `-EIO` and draining stops immediately. If `state=reset` occurs during drain, `read()` returns EOF (`0`) and the daemon must re-open the wire device before accepting the next simulator connection.

## Testing

```sh
# Prerequisite: virtrtlab_core and virtrtlab_uart loaded; virtrtlabd running.
# Connect a terminal to the simulated UART
socat - UNIX-CONNECT:/run/virtrtlab/uart0.sock

# Loopback test: relay bytes between two UART instances via a custom simulator
# (virtrtlabd relays each socket independently; cross-connecting requires a
# userspace process that reads from uart0.sock and writes to uart1.sock)
python3 -c "
import socket, select
s0 = socket.socket(socket.AF_UNIX); s0.connect('/run/virtrtlab/uart0.sock')
s1 = socket.socket(socket.AF_UNIX); s1.connect('/run/virtrtlab/uart1.sock')
while True:
    r, _, _ = select.select([s0, s1], [], [])
    if s0 in r: s1.send(s0.recv(4096))
    if s1 in r: s0.send(s1.recv(4096))
"

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

## Decisions

**Multiple connections per socket — single active connection:** `virtrtlabd` accepts exactly one `connect()` per socket at a time. A second `connect()` attempt is rejected (the process receives `ECONNREFUSED`). No observer/tap mode in v0.1.0.

**Automatic reconnect after simulator disconnect — flush and stay:** when the simulator closes the socket, `virtrtlabd` flushes (discards) any bytes buffered in the wire device for that connection, then immediately returns to `listen()`. The daemon does not restart; the next simulator can `connect()` to a clean slate. Bytes accumulated during the disconnect window are lost.
