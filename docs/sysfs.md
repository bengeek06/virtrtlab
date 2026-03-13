# VirtRTLab sysfs (v1)

Source of truth is the root [README.md](../README.md). This file keeps sysfs-specific details focused.

## Base path

`/sys/kernel/virtrtlab/`

## Root

| Attribute | Access | Type | Description |
|---|---|---|---|
| `version` | ro | string | Semantic version, e.g. `0.1.0` |

Sub-directories: `buses/`, `devices/`.

## Buses

`/sys/kernel/virtrtlab/buses/vrtlbus0/`

| Attribute | Access | Type | Description |
|---|---|---|---|
| `state` | rw | string | `up\|down\|reset` |
| `clock_ns` | ro | u64 | Monotonic snapshot (ns) |
| `seed` | rw | u32 | RNG seed for stochastic fault profiles |

### `state` semantics

| Written value | Behaviour | Value read back |
|---|---|---|
| `up` | Resume data flow on all devices attached to this bus | `up` |
| `down` | Halt TX/RX on all attached devices; pending TX bytes are **drained** to the wire device before halting; AUT `write()` returns `-EIO` while halted | `down` |
| `reset` | One-shot command: drain TX buffers, reset all per-device stats counters, clear all fault injection attrs to `0`; transitions to `up` on completion | `up` |

Writing any other string returns `-EINVAL`. Initial state on module load: `up`.

**Bus `down` with no daemon attached — decided (Option A):** when `state` is set to `down` and the wire device is not open, TX bytes in the circular buffer are dropped immediately. Draining to a closed wire device would stall indefinitely. `stats/drops` is incremented by the byte count discarded.

## Devices — common attrs

`/sys/kernel/virtrtlab/devices/<dev>/`

| Attribute | Access | Type | Description |
|---|---|---|---|
| `type` | ro | string | `uart\|can\|spi\|adc\|dac\|…` |
| `bus` | ro | string | Parent bus, e.g. `vrtlbus0` |
| `enabled` | rw | bool | `0\|1` — gate all data flow |
| `latency_ns` | rw | u64 | Base TX latency added to every transfer (ns) |
| `jitter_ns` | rw | u64 | Uniform jitter amplitude (ns); sampled as a uniform random value in $[0, \text{jitter\_ns}]$ and added to `latency_ns` |
| `drop_rate_ppm` | rw | u32 | Drops per million bytes/frames |
| `bitflip_rate_ppm` | rw | u32 | Bit flips per million bytes/frames |

> **Fault injection direction (v0.1.0)**: `latency_ns`, `jitter_ns`, `drop_rate_ppm`, and `bitflip_rate_ppm` apply on the **TX path only** (bytes flowing from the AUT toward the wire device). RX-direction injection (simulator → AUT) is deferred to v0.2.0.

> **Removed in v1**: `mode` (normal/record/replay) and `fault_policy` — record/replay and policy management are handled in userspace scripts, not the kernel.

## Devices — UART (`uart0`, `uart1`, …)

`/sys/kernel/virtrtlab/devices/uartN/`

### Termios mirrors (read-only)

These attributes **reflect** the current termios state set by the AUT via `tcsetattr()`. They are **not writable** — the AUT owns them through the standard TTY API.

| Attribute | Access | Type | Allowed values |
|---|---|---|---|
| `baud` | ro | u32 | e.g. `9600`, `115200`, `460800` |
| `parity` | ro | string | `none\|even\|odd` |
| `databits` | ro | u8 | `5\|6\|7\|8` |
| `stopbits` | ro | u8 | `1\|2` |

**Error behaviour**: if the AUT sets an unsupported speed, `baud` shows `0`.

**Initial state**: before the AUT opens `/dev/ttyVIRTLABx` for the first time (or after a bus `reset`), the attributes reflect the driver's default `termios` (`tty_std_termios`): `baud` = `38400`, `parity` = `none`, `databits` = `8`, `stopbits` = `1`.

### Buffer configuration (read-write)

| Attribute | Access | Type | Default | Constraints |
|---|---|---|---|---|
| `tx_buf_sz` | rw | u32 | `4096` | Must be a power of two; 64 ≤ value ≤ 65536. Non-power-of-two writes return `-EINVAL`. Read back the attr after writing to confirm the accepted value. |
| `rx_buf_sz` | rw | u32 | `4096` | Must be a power of two; 64 ≤ value ≤ 65536. Non-power-of-two writes return `-EINVAL`. Read back the attr after writing to confirm the accepted value. |

Writes take effect on the **next** open of `/dev/ttyVIRTLABx` (not live-resizable while open; deferred to v0.2.0).

> **TX vs RX overflow semantics**: the TX buffer (AUT → wire device) applies **backpressure** — it never evicts bytes; `write()` blocks or returns `-EAGAIN`. The RX buffer (wire device → AUT) applies **overflow-drop** — oldest byte evicted on full, `stats/overruns` incremented.

### Stats (read-only)

`/sys/kernel/virtrtlab/devices/uartN/stats/`

| Attribute | Type | Description |
|---|---|---|
| `tx_bytes` | u64 | Bytes sent from AUT toward the wire device |
| `rx_bytes` | u64 | Bytes received from wire device toward AUT |
| `overruns` | u64 | Bytes dropped due to buffer overflow; incremented by the **count of bytes evicted** per overflow event (not 1 per event) |
| `drops` | u64 | Bytes discarded by fault injection; incremented by the **byte count of each dropped hrtimer burst** (not 1 per drop decision) |

Counters are reset by writing `0` to `stats/reset`.

### Error behaviour

| Condition | Kernel behaviour |
|---|---|
| `write()` to `/dev/ttyVIRTLABx` when TX buffer full (e.g. due to `latency_ns` backpressure) | `O_NONBLOCK`: return `-EAGAIN`; blocking: suspend until `write_room() > 0`. No bytes dropped. |
| `tx_buf_sz`/`rx_buf_sz` write while device open | return `-EBUSY` |
| `tx_buf_sz`/`rx_buf_sz` write out of range or non-power-of-two | return `-EINVAL` |
| `latency_ns`/`jitter_ns` write > 10 000 000 000 ns | return `-EINVAL` |
| `drop_rate_ppm`/`bitflip_rate_ppm` write > 1 000 000 | return `-EINVAL` |
| `enabled` ← `0` while AUT has `/dev/ttyVIRTLABx` open | return `-EIO` from the next AUT `write()`; `read()` returns `0` (EOF) |

## Rationale

**Why are baud/parity/databits/stopbits read-only?**  
The AUT configures the serial line via `tcsetattr()` — this is the standard POSIX API. VirtRTLab mirrors the termios state in sysfs so user scripts and the daemon can observe it, but the AUT remains the sole authority. Allowing writes would create a split-brain scenario.

**Why no `mode` or `fault_policy` in sysfs?**  
Record/replay and named fault profiles are orchestration concepts. They are cleaner to implement in Python scripts that write individual sysfs attrs, rather than encoding policy state in the kernel. This keeps the kernel surface minimal and auditable.

## Decisions

**Buffer live-resize** — deferred to v0.2.0: `tx_buf_sz`/`rx_buf_sz` writes rejected while device is open (`-EBUSY`).

**Baud rate change notification** — not in v0.1.0: `tcsetattr()` updates termios state and the sysfs `baud` attr atomically. `virtrtlabd` reads `baud` from sysfs on demand; no uevent or control byte is generated by the kernel.
