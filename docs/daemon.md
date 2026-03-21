<!-- SPDX-License-Identifier: CC-BY-4.0 -->

# `virtrtlabd` — Daemon User Guide

`virtrtlabd` is the relay daemon for VirtRTLab. It bridges each UART instance between the kernel wire device (`/dev/virtrtlab-wireN`) and a simulator process via an AF_UNIX socket (`/run/virtrtlab/uartN.sock`).

For GPIO peripherals, no daemon is needed — injection is done directly via sysfs.

---

## What it does

For each UART instance N, `virtrtlabd`:

1. Opens `/dev/virtrtlab-wireN` (the kernel-side byte pipe).
2. Creates `/run/virtrtlab/uartN.sock` with mode `0660`, owner `root:virtrtlab`.
3. Relays bytes in both directions using a single `epoll` loop:
   - AUT → ttyVIRTLABx → kernel → wire device → daemon → socket → simulator
   - simulator → socket → daemon → wire device → kernel → ttyVIRTLABx → AUT
4. On simulator disconnect: flushes stale bytes and re-enters `listen()`.
5. On SIGTERM / SIGINT: removes all sockets and exits with code 0.

---

## Starting the daemon

### Via `virtrtlabctl` (recommended)

```sh
# Start as part of the full lab lifecycle
sudo virtrtlabctl up --uart 2

# Or start the daemon independently (modules must be loaded already)
sudo virtrtlabctl daemon start --num-uarts 2
```

### Directly

```sh
sudo virtrtlabd --num-uarts 2 --run-dir /run/virtrtlab
```

The daemon does **not** daemonize itself (no `fork()` + `setsid()`). Run it under `systemd`, a harness script, or a process supervisor.

---

## Command-line options

```
virtrtlabd --num-uarts N [--run-dir DIR]
```

| Option | Default | Description |
|---|---|---|
| `--num-uarts N` | `1` | Number of UART instances to relay. Must match the number loaded by `virtrtlab_uart num_uart_devices=N`. |
| `--run-dir DIR` | `/run/virtrtlab` | Directory where sockets and the PID file are created. Must exist and be writable by root. |

**Example:**

```sh
# Relay 4 UART instances, sockets in a custom directory
sudo virtrtlabd --num-uarts 4 --run-dir /tmp/virtrtlab-test
```

---

## Socket permissions

Sockets are created with:

- Mode: `0660` (owner and group can read/write; no world access)
- Owner: `root:virtrtlab`

This means any user in the `virtrtlab` group can connect a simulator without `sudo`. See [Installation](../README.md#system-setup) for group setup.

---

## Signals

| Signal | Behaviour |
|---|---|
| `SIGTERM` | Clean shutdown: removes all sockets, exits 0 |
| `SIGINT` | Same as SIGTERM (Ctrl-C in foreground) |
| `SIGPIPE` | Ignored globally — broken pipe on a socket returns `EPIPE` instead of killing the daemon |

The daemon uses `signalfd(2)` to handle signals as epoll events. There are no async signal handlers.

---

## Logging

`virtrtlabd` logs to **syslog** (`LOG_DAEMON` facility) and to **stderr** when run in the foreground.

```sh
# View daemon logs in real time
sudo journalctl -f -t virtrtlabd

# Or with syslog
sudo tail -f /var/log/syslog | grep virtrtlabd
```

Key log messages:

| Message | Meaning |
|---|---|
| `starting: num_uarts=N run_dir=DIR` | Daemon is starting |
| `group 'virtrtlab' not found — sockets will be root:root 0660` | `virtrtlab` group missing; sockets are accessible to root only |
| `uart0: bind /run/virtrtlab/uart0.sock: …` | Socket creation failed |
| `uart0: simulator connected` | A simulator process connected |
| `uart0: simulator disconnected, flushing` | Simulator closed the socket; daemon is flushing |
| `shutdown: removing sockets` | Clean shutdown in progress |

---

## Reconnect behaviour

When a simulator disconnects (socket EOF):

1. The daemon flushes any stale AUT→simulator bytes in the wire buffer (discards them silently).
2. The wire device stays open — no module reload required.
3. The daemon immediately re-enters `listen()` on `uartN.sock`.
4. The next simulator can `connect()` to a clean slate.

**Bytes accumulated during the disconnect window are lost.** This is intentional: delivering stale bytes from a previous session to a new simulator would corrupt the stream.

---

## Wire device re-open on bus reset

If the virtual bus is reset (`virtrtlabctl set vrtlbus0 state=reset`), the wire device delivers EOF to `read()`. The daemon detects this and:

1. Closes and re-opens `/dev/virtrtlab-wireN`.
2. Disconnects the current simulator (if any).
3. Returns to `WAIT_CLIENT` state.

This allows the bus to be reset between test runs without restarting the daemon.

---

## Running under systemd

```ini
# /etc/systemd/system/virtrtlabd.service
[Unit]
Description=VirtRTLab UART relay daemon
After=network.target

[Service]
Type=simple
ExecStart=/usr/local/sbin/virtrtlabd --num-uarts 1 --run-dir /run/virtrtlab
Restart=on-failure
RuntimeDirectory=virtrtlab
RuntimeDirectoryMode=0755

[Install]
WantedBy=multi-user.target
```

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now virtrtlabd
sudo systemctl status virtrtlabd
```

---

## Testing the socket directly

**Connect a terminal to the simulated UART:**

```sh
socat - UNIX-CONNECT:/run/virtrtlab/uart0.sock
```

**Inspect the byte stream:**

```sh
socat - UNIX-CONNECT:/run/virtrtlab/uart0.sock | xxd
```

**Replay a captured session:**

```sh
cat capture.bin | socat - UNIX-CONNECT:/run/virtrtlab/uart0.sock
```

**Cross-connect two UART instances** (loopback test):

```python
import socket, select

s0 = socket.socket(socket.AF_UNIX)
s0.connect('/run/virtrtlab/uart0.sock')
s1 = socket.socket(socket.AF_UNIX)
s1.connect('/run/virtrtlab/uart1.sock')

while True:
    r, _, _ = select.select([s0, s1], [], [])
    if s0 in r:
        s1.sendall(s0.recv(4096))
    if s1 in r:
        s0.sendall(s1.recv(4096))
```

---

## Performance notes

- **Single epoll instance** shared across all UART instances. `epoll_wait()` manages at most `3 × num_uarts` fds (wire + server + client per instance), O(1) regardless of count.
- **Static relay buffers**: 4096 bytes per direction per instance, allocated once at startup. No heap allocation in the relay hot path.
- **RSS footprint**: under 512 KB for 8 instances.
- **Startup latency**: under 5 ms.

These properties are intentional: the daemon must not introduce jitter that would interfere with the timing measurements VirtRTLab is designed to perform on the AUT.

---

## Architecture reference

For the full daemon state machine, epoll loop design, signal handling, and rationale, see [socket-api.md](socket-api.md) and [spec.md](spec.md).
