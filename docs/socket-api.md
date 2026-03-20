<!-- SPDX-License-Identifier: CC-BY-4.0 -->

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

## Implementation

`virtrtlabd` is implemented in **C (GNU11)**. The daemon sits in the data path for all UART instances simultaneously; GC pauses and interpreter startup overhead are incompatible with the latency and resource budgets of VirtRTLab. The sections below specify the source layout, event loop, buffer strategy, and signal handling that implementations must follow.

### Source layout

```
daemon/
├── main.c          — argument parsing, optional daemonize, signal setup, top-level lifecycle
├── epoll_loop.c    — shared epoll instance; registers/unregisters fds; dispatches events
├── epoll_loop.h
├── instance.c      — per-UART state machine (WAIT_CLIENT → RELAYING → DRAINING)
├── instance.h      — struct uart_instance definition
└── Makefile        — CFLAGS = -Wall -Wextra -O2 -std=gnu11; links against libc only
```

### Per-instance state machine

Each UART instance N owns:

- `wire_fd` — file descriptor for `/dev/virtrtlab-wireN` (opened at daemon start, re-opened on reset)
- `server_fd` — listening `AF_UNIX SOCK_STREAM` socket bound to `/run/virtrtlab/uartN.sock`
- `client_fd` — connected simulator socket; `-1` when no simulator is present

| State | Active fds monitored by epoll | Transition |
|---|---|---|
| `WAIT_CLIENT` | `server_fd` (EPOLLIN) | `accept()` succeeds → `RELAYING` |
| `RELAYING` | `wire_fd` (EPOLLIN), `client_fd` (EPOLLIN) | client EOF → drain inline → `WAIT_CLIENT`; wire EOF → reopen wire → `WAIT_CLIENT` |

On simulator disconnect, stale wire bytes are drained **inline** (not via a separate epoll state): `wire_fd` is set `O_NONBLOCK` and read in a loop until `EAGAIN`/`EIO`/EOF, then flags are restored before re-entering `WAIT_CLIENT`. An epoll-driven `DRAINING` state was considered but rejected: `EPOLLIN` never fires on an empty wire device when the AUT is silent, causing an indefinite wait. The inline drain is bounded by the kernel ring buffer (~4 KB).

### epoll loop

A **single `epoll` instance** is shared across all UART instances. `select()` and `poll()` are not used. With `num_uarts` up to 8, `epoll_wait()` manages at most 3 fds per instance (wire + server + client) = 24 fds, O(1) regardless of count.

Each fd is registered with a pointer to a small dispatch context in `epoll_event.data.ptr`:

```c
struct evt_ctx {
    struct uart_instance *inst;
    enum fd_role role;          /* ROLE_WIRE | ROLE_SERVER | ROLE_CLIENT | ROLE_SIGNAL */
};
```

The main loop is:

```c
epoll_fd = epoll_create1(EPOLL_CLOEXEC);

/* Register the signal fd once, outside the per-instance loop. */
epoll_ctl(epoll_fd, EPOLL_CTL_ADD, sig_fd, &ev_signal);

/* For each instance N at startup: */
for (int n = 0; n < num_uarts; n++) {
    epoll_ctl(epoll_fd, EPOLL_CTL_ADD, inst[n].server_fd, &ev_server);
    /* wire_fd is NOT added here: only server_fd is watched in WAIT_CLIENT. */
}

for (;;) {
    n = epoll_wait(epoll_fd, events, MAX_EVENTS, -1);
    for (i = 0; i < n; i++)
        dispatch(events[i].data.ptr, events[i].events);
}
```

### Static buffers

Each instance owns two byte arrays allocated once at startup — no heap allocation occurs in the relay hot path:

| Field | Size | Direction |
|---|---|---|
| `inst->wire_buf[4096]` | 4096 B | wire → client (AUT → simulator) |
| `inst->sock_buf[4096]` | 4096 B | client → wire (simulator → AUT) |

4096 bytes matches the default TX/RX FIFO size of the wire device. If a single `read()` returns fewer bytes, the partial result is forwarded immediately — no coalescing, no dynamic sizing.

### Signal handling — `signalfd`

Signals are handled via `signalfd(2)`, not async `sigaction` handlers. SIGTERM and SIGINT become ordinary file-descriptor events in the epoll loop, with no async-signal-safety constraints:

```c
sigset_t mask;
sigemptyset(&mask);
sigaddset(&mask, SIGTERM);
sigaddset(&mask, SIGINT);
sigprocmask(SIG_BLOCK, &mask, NULL);            /* block normal delivery to the process */
sig_fd = signalfd(-1, &mask, SFD_CLOEXEC);     /* readable when a signal arrives */
epoll_ctl(epoll_fd, EPOLL_CTL_ADD, sig_fd, &ev_signal);
```

On `SIGTERM` or `SIGINT`, the dispatch function:
1. Calls `unlink("/run/virtrtlab/uartN.sock")` for every instance
2. Closes all fds in reverse order of creation (`client_fd`, `server_fd`, `wire_fd`, `sig_fd`, `epoll_fd`)
3. Exits with `EXIT_SUCCESS`

### Relay behaviour

**RELAYING — `wire_fd` readable (AUT → simulator)**

```
n = read(wire_fd, wire_buf, sizeof wire_buf)
n < 0, errno == EIO   → usleep(10000); continue           /* state=down: fd valid, bus halted */
n == 0                → close(wire_fd); wire_fd = reopen  /* state=reset: fd invalidated */
                        close(client_fd); client_fd = -1
                        epoll: remove client_fd, keep server_fd → WAIT_CLIENT
n > 0                 → write(client_fd, wire_buf, n)     /* forward to simulator */
```

**RELAYING — `client_fd` readable (simulator → AUT)**

```
n = recv(client_fd, sock_buf, sizeof sock_buf, 0)
n == 0  → close(client_fd); client_fd = -1
           inline drain (see above) → WAIT_CLIENT
n > 0   → write(wire_fd, sock_buf, n)
           /* EAGAIN: RX buffer full → byte lost, stat_overruns incremented by kernel */
```

**Inline drain on simulator disconnect**

On simulator disconnect, the daemon discards stale AUT→daemon bytes so the next simulator starts from a clean stream. `wire_fd` stays open throughout to preserve bus state change notifications. The drain is done inline (not via a separate epoll state) using `O_NONBLOCK`:

```
fl = fcntl(wire_fd, F_GETFL, 0)
fcntl(wire_fd, F_SETFL, fl | O_NONBLOCK)   /* set non-blocking for drain */

loop:
    n = read(wire_fd, wire_buf, sizeof wire_buf)
    n > 0                   → discard silently, continue
    n == 0                  → close(wire_fd); wire_fd = reopen  /* state=reset during drain */
                               break
    n < 0, errno == EAGAIN  → drain complete; break
    n < 0, errno == EIO     → drain stops (bus went down); break

fcntl(wire_fd, F_SETFL, fl)   /* restore original flags */
→ WAIT_CLIENT
```

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

**Why C (GNU11) and not Python for the daemon?**  
The daemon is in the data path for every UART instance. CPython's garbage collector introduces multi-millisecond pauses at unpredictable intervals — exactly the kind of jitter that VirtRTLab is designed to *measure* in the AUT, not inject at the infrastructure level. A C process with static buffers has an RSS footprint under 512 KB for 8 instances; a Python interpreter starts at 20–50 MB. Build-time dependency: libc only. Startup latency: < 5 ms (vs. 50–200 ms for CPython), which matters in CI where modules are loaded and unloaded repeatedly.

**Why raw bytes instead of JSONL?**  
The AUT exchanges raw bytes with the simulated UART. Wrapping bytes in JSONL would require an extra encode/decode step in the daemon and add latency. Fault injection (drops, bitflips) operates at the byte level in the kernel — the userspace socket sees the already-mutated stream.

**Why per-device sockets instead of a multiplexed global socket?**  
Per-device sockets let the simulator process use a plain `connect()` to `/run/virtrtlab/uart0.sock` with no demultiplexing logic. This also matches what `socat` and raw POSIX tools expect.

## Decisions

**Multiple connections per socket — single active connection:** `virtrtlabd` accepts exactly one `connect()` per socket at a time. A second `connect()` attempt is rejected (the process receives `ECONNREFUSED`). No observer/tap mode in v0.1.0.

**Automatic reconnect after simulator disconnect — flush and stay:** when the simulator closes the socket, `virtrtlabd` flushes (discards) any bytes buffered in the wire device for that connection, then immediately returns to `listen()`. The daemon does not restart; the next simulator can `connect()` to a clean slate. Bytes accumulated during the disconnect window are lost.

**Implementation language — C (GNU11):** `virtrtlabd` must be written in C (GNU11), compiled with `-Wall -Wextra -O2`, and link only against libc. Python or other scripting languages are excluded from the daemon binary. `virtrtlabctl` (the control CLI) remains in Python as it is not in the data path.
