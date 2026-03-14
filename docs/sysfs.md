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
| `clock_ns` | ro | u64 | `CLOCK_MONOTONIC` snapshot (ns) taken at the moment the attr is read; nanosecond-precision via `ktime_get_ns()`, not driven by the TX hrtimer |
| `seed` | rw | u32 | RNG seed for stochastic fault profiles; the xorshift32 PRNG is re-seeded on write; one PRNG draw per drop/bitflip decision |

### `state` semantics

| Written value | Behaviour | Value read back |
|---|---|---|
| `up` | Resume data flow on all devices attached to this bus | `up` |
| `down` | Halt TX/RX on all attached devices. Pending TX bytes: **drained** to the wire device if the daemon has it open; **dropped immediately** (incrementing `stats/drops`) if no daemon is attached. AUT `write()` returns `-EIO` while halted. Wire device: `poll()` asserts `POLLHUP \| POLLERR` and de-asserts `POLLIN`/`POLLOUT`; `read(wire_fd)` and `write(wire_fd)` return `-EIO`. After `state=up`, the wire fd remains valid — no re-open required. AUT `open("/dev/ttyVIRTLABx")` succeeds even when down; subsequent read/write return `-EIO`. | `down` |
| `reset` | One-shot command: (1) drop pending TX bytes; deliver EOF to any open wire_fd (`read()` returns `0`, `poll()` asserts `POLLHUP`) — the daemon must `close()` + re-`open()` the wire device; (2) reset all per-device stats counters to `0`; (3) clear `latency_ns`, `jitter_ns`, `drop_rate_ppm`, `bitflip_rate_ppm` to `0`; (4) set `enabled` to `1` on all attached devices; (5) transition to `up`. `tx_buf_sz`, `rx_buf_sz`, and termios mirrors are unaffected. | `up` |

Writing any other string returns `-EINVAL`. Initial state on module load: `up`.

When `state` is set to `down` and the wire device is not open: pending TX bytes are **dropped immediately** (`stats/drops` incremented). Drain would stall indefinitely with no consumer.

**`state` vs `enabled` interaction**: `state` is a bus-level gate; `enabled` is a device-level gate. Both must be active for data to flow through a device. `state=down` overrides `enabled=1` — all devices on the bus halt regardless of their individual `enabled` value. `state=up` restores the bus gate but does **not** modify per-device `enabled`; a device that had `enabled=0` before `state=down` remains halted after `state=up`. Only `state=reset` forces `enabled=1` on all attached devices (step 4 above).

## Devices — common attrs

`/sys/kernel/virtrtlab/devices/<dev>/`

| Attribute | Access | Type | Description |
|---|---|---|---|
| `type` | ro | string | `uart\|gpio\|spi\|adc\|dac\|…` |
| `bus` | ro | string | Parent bus, e.g. `vrtlbus0` |
| `enabled` | rw | bool | `0\|1` — gate all data flow; default `1` |
| `latency_ns` | rw | u64 | Base TX latency per burst (ns); default `0` |
| `jitter_ns` | rw | u64 | Uniform jitter amplitude (ns); sampled as a uniform random value in $[0, \text{jitter\_ns}]$ added to `latency_ns`; default `0` |
| `drop_rate_ppm` | rw | u32 | Drops per million bytes; default `0` |
| `bitflip_rate_ppm` | rw | u32 | Bit flips per million bytes; default `0` |

> **Fault injection direction (v0.1.0)**: `latency_ns`, `jitter_ns`, `drop_rate_ppm`, and `bitflip_rate_ppm` apply on the **TX path only** (bytes flowing from the AUT toward the wire device). RX-direction injection (simulator → AUT) is deferred to v0.2.0.

> **Burst definition (v0.1.0)**: one hrtimer callback = one character. The hrtimer fires at the character period `period_ns = ⌈bits_per_frame × 10⁹ / baud⌉` where `bits_per_frame = 1 (start) + databits + stopbits + (1 if parity ≠ none else 0)`. Each callback dequeues exactly one byte from the TX buffer (if non-empty), applies fault injection to it, and delivers it to the wire device.

> **`latency_ns` / `jitter_ns` semantics**: after a burst is delivered, the hrtimer is re-armed at `now + period_ns + latency_ns + uniform(0, jitter_ns)`. When `latency_ns=0` and `jitter_ns>0`, jitter still applies — there is no fast-path bypass for zero base latency. These delays affect when bytes reach the wire device; the AUT's `write()` sees backpressure only when the TX circular buffer fills.

> **Fault attr update timing**: writes to `latency_ns`, `jitter_ns`, `drop_rate_ppm`, or `bitflip_rate_ppm` take effect from the **next hrtimer callback** after the sysfs store returns; no in-flight burst is modified. Store uses `WRITE_ONCE()`; hrtimer callback reads with `READ_ONCE()`.

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

**Error behaviour**: if `tty_termios_baud_rate()` returns `0` for the speed set by the AUT (unsupported baud value or `B0`), `baud` shows `0`.

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
| `tx_bytes` | u64 | Bytes received from the AUT, counted **before** fault injection. `tx_bytes − drops ≈ bytes actually delivered to the wire device`. |
| `rx_bytes` | u64 | Bytes received from wire device toward AUT |
| `overruns` | u64 | **RX buffer only**: bytes evicted from the RX buffer (wire device → AUT) on overflow; incremented by the count of bytes evicted per overflow event. TX buffer never evicts — it applies backpressure instead. |
| `drops` | u64 | Bytes discarded by fault injection (`drop_rate_ppm`) or by `state=down` with no daemon; incremented by the byte count of each dropped hrtimer burst. |

Counters are reset by writing `0` to `stats/reset`. Counters wrap silently at `UINT64_MAX` (modular arithmetic, no saturation).

> **Counter units (UART)**: all four counters measure individual **bytes**. For future peripheral types (CAN, SPI, …), counter units are type-specific and documented in each peripheral's spec section; the common-attrs table does not define units.

### Error behaviour

| Condition | Kernel behaviour |
|---|---|
| `open("/dev/ttyVIRTLABx")` when bus `state=down` | succeeds (returns a valid fd); subsequent `read()`/`write()` on that fd return `-EIO` until `state=up` |
| `write()` to `/dev/ttyVIRTLABx` when TX buffer full (e.g. due to `latency_ns` backpressure) | `O_NONBLOCK`: return `-EAGAIN`; blocking: suspend until `write_room() > 0`. No bytes dropped. |
| `tx_buf_sz`/`rx_buf_sz` write while device open | return `-EBUSY` |
| `tx_buf_sz`/`rx_buf_sz` write out of range or non-power-of-two | return `-EINVAL` |
| `latency_ns`/`jitter_ns` write > 10 000 000 000 ns | return `-EINVAL` |
| `drop_rate_ppm`/`bitflip_rate_ppm` write > 1 000 000 | return `-EINVAL` |
| `enabled` ← `0` while AUT has `/dev/ttyVIRTLABx` open | return `-EIO` from the next AUT `write()`; a `write()` already sleeping in the TTY layer completes normally. `read()` drains any bytes already in the RX buffer, then returns `0` (EOF) |
| `stats/reset` write value other than `0` | return `-EINVAL` |
| `read()` on `stats/reset` | returns `-EPERM` (write-only attribute; no `show()` callback registered) |

## Devices — GPIO (`gpio0`, `gpio1`, …)

`v0.1.0` includes `virtrtlab_gpio` as the second reference peripheral family. Unlike UART, GPIO is intentionally **state-oriented** rather than stream-oriented and does not require the daemon socket path in the MVP.

Minimum expected observable surface before implementation starts:

| Attribute | Access | Type | Allowed values |
|---|---|---|---|
| `direction` | rw | string | `in\|out` |
| `value` | rw/ro | bool | `0\|1` |
| `active_low` | rw | bool | `0\|1` |
| `edge` | rw | string | `none\|rising\|falling\|both` |
| `stats/` | ro | directory | transition and error counters |

> **Open:** define whether `virtrtlab_gpio` models one device per GPIO line or one device per GPIO bank, how line naming maps to `/sys/kernel/virtrtlab/devices/gpioN/`, and the exact counter set exposed under `stats/`.

> **Open:** define the precise interaction between common fault attrs (`latency_ns`, `jitter_ns`, `drop_rate_ppm`, `bitflip_rate_ppm`) and GPIO semantics. For GPIO, `drop_rate_ppm` and `bitflip_rate_ppm` likely translate to suppressed or inverted value transitions rather than byte-level mutation.

## Rationale

**Why are baud/parity/databits/stopbits read-only?**  
The AUT configures the serial line via `tcsetattr()` — this is the standard POSIX API. VirtRTLab mirrors the termios state in sysfs so user scripts and the daemon can observe it, but the AUT remains the sole authority. Allowing writes would create a split-brain scenario.

**Why no `mode` or `fault_policy` in sysfs?**  
Record/replay and named fault profiles are orchestration concepts. They are cleaner to implement in Python scripts that write individual sysfs attrs, rather than encoding policy state in the kernel. This keeps the kernel surface minimal and auditable.

## Decisions

**Buffer live-resize** — deferred to v0.2.0: `tx_buf_sz`/`rx_buf_sz` writes rejected while device is open (`-EBUSY`).

**Baud rate change notification** — not in v0.1.0: `tcsetattr()` updates termios state and the sysfs `baud` attr atomically. `virtrtlabd` reads `baud` from sysfs on demand; no uevent or control byte is generated by the kernel.

**PRNG scope** — the xorshift32 state lives at the bus level (`buses/vrtlbus0/seed`), shared across all devices on that bus. Devices on the same bus draw from the shared state in interleaved order; each device does not maintain its own PRNG. For reproducible CI results, write `seed` before activating stochastic fault injection and record it in test artifacts.
