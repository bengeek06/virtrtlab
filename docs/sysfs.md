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
| `latency_ns` | rw | u64 | Base delivery latency per transfer unit (ns); default `0` |
| `jitter_ns` | rw | u64 | Uniform jitter amplitude (ns); sampled as a uniform random value in $[0, \text{jitter\_ns}]$ added to `latency_ns`; default `0` |
| `drop_rate_ppm` | rw | u32 | Drops per million transfer units; default `0` |
| `bitflip_rate_ppm` | rw | u32 | Payload corruptions or inverted transitions per million transfer units; default `0` |

> **Fault injection direction (v0.1.0)**: the common fault attrs apply to the **AUT-driven transmit path** for UART and to the **sysfs-injected input-transition path** for GPIO. They do not mutate simulator → AUT UART traffic or AUT-driven GPIO output transitions in `v0.1.0`.

> **Transfer unit definition (v0.1.0)**: transfer-unit granularity is peripheral-specific. For UART, one transfer unit is one byte paced by the hrtimer. For GPIO, one transfer unit is one sysfs write to `value`, potentially affecting multiple bits of the same bank.

> **`latency_ns` / `jitter_ns` semantics**: delivery timing is peripheral-specific. For UART, these attrs delay byte delivery toward the wire device and therefore influence TX backpressure. For GPIO, they delay sysfs-injected input transitions before the logical line state changes; see the GPIO section below.

> **Fault attr update timing**: writes to `latency_ns`, `jitter_ns`, `drop_rate_ppm`, or `bitflip_rate_ppm` take effect from the **next transfer scheduling point** after the sysfs store returns. For UART, that means the next hrtimer callback; for GPIO, the next sysfs write to `value`. No already-scheduled transfer unit is modified in place.

> **GPIO delayed-write snapshot rule**: when a GPIO `value` write is accepted for delayed delivery, the kernel snapshots the requested logical bank value together with the current `latency_ns`, `jitter_ns`, `drop_rate_ppm`, `bitflip_rate_ppm`, `direction`, and `active_low` state before the sysfs store returns. A later change to those attrs does not rewrite a bank update that was already accepted and scheduled.

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

### Device model

Each `gpioN` device models **one logical bank of 8 lines**. There is no per-line device in `v0.1.0`. Bank numbering is 0-based and follows the `num_gpio_banks` module parameter.

Ownership rules:

- when a `direction` bit is `0`, the AUT observes that bit as an input and userspace may drive it by writing `value` through sysfs
- when a `direction` bit is `1`, the AUT drives that bit as an output and sysfs can only observe it
- `value`, edge detection, and all counters are expressed in **logical** bank state after `active_low` has been applied

### Attributes

| Attribute | Access | Type | Allowed values | Description |
|---|---|---|---|---|
| `direction` | rw | u8 mask | `0x00..0xFF` | Per-bit ownership mask. `1` = AUT output, `0` = AUT input |
| `value` | rw/ro | u8 mask | `0x00..0xFF` | Read returns the current logical bank value. Write injects a logical bank value toward AUT-input bits only. Bits owned by the AUT are ignored on write and remain unchanged |
| `active_low` | rw | u8 mask | `0x00..0xFF` | Per-bit logical inversion mask applied to readback, writes, and edge matching |
| `edge_rising` | rw | u8 mask | `0x00..0xFF` | Per-bit mask enabling rising-edge event detection on AUT-input bits |
| `edge_falling` | rw | u8 mask | `0x00..0xFF` | Per-bit mask enabling falling-edge event detection on AUT-input bits |
| `stats/value_changes` | ro | u64 | n/a | Count of logical line transitions actually applied after fault handling |
| `stats/edge_events` | ro | u64 | n/a | Count of individual bit transitions that match the enabled edge masks |
| `stats/drops` | ro | u64 | n/a | Count of dropped `value` write operations suppressed by `drop_rate_ppm` |
| `stats/reset` | wo | u8 | `0` | Writing `0` resets all GPIO counters atomically; any other value returns `-EINVAL` |

Mask-format rules for `direction`, `value`, `active_low`, `edge_rising`, and `edge_falling`:

- Writes accept only lowercase or uppercase hexadecimal in the exact form `0xNN`, where `NN` is two hex digits
- Decimal, octal, signed, whitespace-padded, or shortened forms such as `1`, `01`, `0x1`, and `255` return `-EINVAL`
- Reads return the canonical lowercase form `0xnn` followed by `\n`

Bit numbering is LSB-first: bit 0 is mask `0x01`, bit 7 is mask `0x80`.

All GPIO masks and counters are defined in the **logical** domain after `active_low` has been applied. In particular, a userspace write to `value` expresses the requested logical bank state, `value` readback reports the logical bank state, edge matching is performed on logical transitions, and `bitflip_rate_ppm` flips one random AUT-input bit in that logical bank value before delivery.

### Fault attribute semantics

For GPIO, the common fault attrs apply only to **sysfs writes to `value`** and only to bits configured as AUT inputs:

- `latency_ns` delays delivery of the bank update
- `jitter_ns` adds uniform delay variation on top of `latency_ns`
- `drop_rate_ppm` suppresses the whole bank write, leaves the logical bank value unchanged, and increments `stats/drops`
- `bitflip_rate_ppm` flips one random AUT-input bit within the requested bank value before delivery. If the flipped value produces no effective bit transition, the write succeeds but `stats/value_changes` does not increment for that bit

These attrs do not alter AUT-driven output transitions in `v0.1.0`.

If a delayed GPIO bank write is pending, all four decisions above are made from the snapshotted attribute values captured when that `value` write was accepted; later sysfs writes affect only subsequently accepted bank writes.

### Direction, edge, and reset semantics

- Writes to `edge_rising` and `edge_falling` are masked by `~direction`; output-owned bits are stored as `0`
- A sysfs write to `value` updates only AUT-input bits; output-owned bits read back exactly as last driven by the AUT
- Writes to `value` while the device is disabled (`enabled=0`) or while the bus state is `down` return `-EIO`
- Bus `state=reset` clears `latency_ns`, `jitter_ns`, `drop_rate_ppm`, `bitflip_rate_ppm`, resets all GPIO counters, sets `enabled=1`, and preserves `direction`, `active_low`, edge masks, and the current logical bank value

Counter units for GPIO are intentionally mixed and must be read literally:

- `stats/value_changes` counts individual logical bit transitions that were actually applied to AUT-input bits
- `stats/edge_events` counts individual logical bit transitions that matched the enabled edge masks
- `stats/drops` counts suppressed `value` write operations, one increment per dropped bank write regardless of how many bits that write would have changed

### Error behaviour

The GPIO error model is based on a **standard memory-mapped banked GPIO controller** as exposed through Linux `gpiolib`, with Xilinx AXI GPIO used as a representative reference shape for `v0.1.0`. VirtRTLab specifies only the observable sysfs contract below; legacy `/sys/class/gpio` export semantics and line-reservation errors are out of scope.

| Condition | Kernel behaviour |
|---|---|
| `direction`, `value`, `active_low`, `edge_rising`, or `edge_falling` write not matching the strict `0xNN` format | return `-EINVAL` |
| `latency_ns`/`jitter_ns` write > 10 000 000 000 ns | return `-EINVAL` |
| `drop_rate_ppm`/`bitflip_rate_ppm` write > 1 000 000 | return `-EINVAL` |
| `stats/reset` write value other than `0` | return `-EINVAL` |
| `read()` on `stats/reset` | returns `-EPERM` (write-only attribute; no `show()` callback registered) |
| `value` write while `enabled=0` | return `-EIO` |
| `value` write while bus `state=down` | return `-EIO` |
| `value` write with one or more bits owned by the AUT (`direction=1`) | succeeds; those output-owned bits are ignored and retain the last AUT-driven logical state |
| `edge_rising`/`edge_falling` write with one or more bits owned by the AUT (`direction=1`) | succeeds; those output-owned bits are stored as `0` |

No `-EBUSY` condition is specified for GPIO in `v0.1.0`: VirtRTLab does not model per-line userspace export, descriptor reservation, or exclusive IRQ ownership at the sysfs interface level.

### Test-oriented examples

Drive one input bit high and count a rising edge:

```sh
echo 0x00 > /sys/kernel/virtrtlab/devices/gpio0/direction
echo 0x01 > /sys/kernel/virtrtlab/devices/gpio0/edge_rising
echo 0x01 > /sys/kernel/virtrtlab/devices/gpio0/value
cat /sys/kernel/virtrtlab/devices/gpio0/stats/value_changes
cat /sys/kernel/virtrtlab/devices/gpio0/stats/edge_events
```

Expected result: if bit 0 was previously low, both counters increment by 1.

Drop a whole bank write:

```sh
echo 0x00 > /sys/kernel/virtrtlab/devices/gpio0/direction
echo 1000000 > /sys/kernel/virtrtlab/devices/gpio0/drop_rate_ppm
echo 0x55 > /sys/kernel/virtrtlab/devices/gpio0/value
cat /sys/kernel/virtrtlab/devices/gpio0/value
cat /sys/kernel/virtrtlab/devices/gpio0/stats/drops
```

Expected result: `value` remains unchanged and `stats/drops` increments by 1.

Observe AUT-driven output bits while still driving an input bit from sysfs:

```sh
echo 0x0F > /sys/kernel/virtrtlab/devices/gpio0/direction
echo 0x80 > /sys/kernel/virtrtlab/devices/gpio0/value
cat /sys/kernel/virtrtlab/devices/gpio0/value
```

Expected result: bits 0..3 reflect the AUT-driven output state, and only bit 7 may be changed by the sysfs write.

## Rationale

**Why are baud/parity/databits/stopbits read-only?**  
The AUT configures the serial line via `tcsetattr()` — this is the standard POSIX API. VirtRTLab mirrors the termios state in sysfs so user scripts and the daemon can observe it, but the AUT remains the sole authority. Allowing writes would create a split-brain scenario.

**Why no `mode` or `fault_policy` in sysfs?**  
Record/replay and named fault profiles are orchestration concepts. They are cleaner to implement in Python scripts that write individual sysfs attrs, rather than encoding policy state in the kernel. This keeps the kernel surface minimal and auditable.

**Why an 8-bit bank instead of one device per line?**  
An 8-bit bank is a pragmatic MVP shape: it matches common embedded register-style GPIO usage, keeps the sysfs surface compact, and allows simultaneous bit transitions without inventing a more complex userspace protocol.

**Why use a `gpiolib` / Xilinx-style error model?**  
For `v0.1.0`, VirtRTLab needs a simple GPIO contract that feels familiar to Linux driver authors and test engineers. A banked memory-mapped controller such as Xilinx AXI GPIO is representative for per-bit direction, bank value read/write, and edge-capable input lines. The spec therefore adopts that controller family as an inspiration for observable error cases, while intentionally excluding Linux legacy sysfs-export workflow details that do not match the VirtRTLab sysfs surface.

## Decisions

**Buffer live-resize** — deferred to v0.2.0: `tx_buf_sz`/`rx_buf_sz` writes rejected while device is open (`-EBUSY`).

**Baud rate change notification** — not in v0.1.0: `tcsetattr()` updates termios state and the sysfs `baud` attr atomically. `virtrtlabd` reads `baud` from sysfs on demand; no uevent or control byte is generated by the kernel.

**PRNG scope** — the xorshift32 state lives at the bus level (`buses/vrtlbus0/seed`), shared across all devices on that bus. Devices on the same bus draw from the shared state in interleaved order; each device does not maintain its own PRNG. For reproducible CI results, write `seed` before activating stochastic fault injection and record it in test artifacts.

**GPIO bank width** — fixed at 8 bits in `v0.1.0`: each `gpioN` instance models exactly 8 logical lines. Wider banks or configurable bank widths are deferred.
