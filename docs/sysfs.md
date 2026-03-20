<!-- SPDX-License-Identifier: CC-BY-4.0 -->

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
| `seed` | rw | u32 | xorshift32 PRNG seed for stochastic fault profiles. **Read**: returns the current internal PRNG state (not necessarily the value last written — draws advance the state). **Write**: immediately replaces the internal state with the written value; `0` is invalid and returns `-EINVAL`. On `insmod`, the PRNG is initialised to `1` (hard-coded; always identical after a reload). `state=reset` does **not** affect the PRNG — to reset to a known sequence, write `seed` explicitly (e.g. `echo 1 > seed`). Drop decisions consume one PRNG draw per burst. Bitflip decisions consume one draw for the gate+byte-index check and, when the gate fires, a second draw for the bit position. |

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
| `enabled` | rw | bool | `0\|1` — device-level data flow gate; default `1`. Writing `0` blocks new AUT `write()` calls (return `-EIO`) **and** drains any bytes already queued in the TX buffer as `stats/drops` without delivering them to the wire device. Writing `1` lifts the block; the hrtimer is rearmed on the next AUT `write()`. |
| `latency_ns` | rw | u64 | Base delivery latency per transfer unit (ns); default `0` |
| `jitter_ns` | rw | u64 | Uniform jitter amplitude (ns); sampled as a uniform random value in $[0, \text{jitter\_ns}]$ added to `latency_ns`; default `0` |
| `drop_rate_ppm` | rw | u32 | Drops per million transfer units; default `0` |
| `bitflip_rate_ppm` | rw | u32 | Payload corruptions or inverted transitions per million transfer units; default `0` |

> **Fault injection direction (v0.1.0)**: the common fault attrs apply to the **AUT-driven transmit path** for UART and to the **sysfs-injected input-transition path** for GPIO. They do not mutate simulator → AUT UART traffic or AUT-driven GPIO output transitions in `v0.1.0`.

> **Transfer unit definition (v0.1.0)**: transfer-unit granularity is peripheral-specific. For UART, one transfer unit is one byte paced by the hrtimer. For GPIO, one transfer unit is one sysfs write to `inject`, targeting a single line transition.

> **`latency_ns` / `jitter_ns` semantics**: delivery timing is peripheral-specific. For UART, these attrs delay byte delivery toward the wire device and therefore influence TX backpressure. For GPIO, they delay sysfs-injected input transitions before the logical line state changes; see the GPIO section below.

> **Fault attr update timing**: writes to `latency_ns`, `jitter_ns`, `drop_rate_ppm`, or `bitflip_rate_ppm` take effect from the **next transfer scheduling point** after the sysfs store returns. For UART, that means the next hrtimer callback; for GPIO, the next sysfs write to `inject`. No already-scheduled transfer unit is modified in place.

> **GPIO delayed-write snapshot rule**: when a GPIO `inject` write is accepted for delayed delivery, the kernel snapshots the requested line index, requested physical value, and current `latency_ns`, `jitter_ns`, `drop_rate_ppm`, and `bitflip_rate_ppm` state before the sysfs store returns. A later change to those attrs does not rewrite a transition that was already accepted and scheduled.

> **`enabled=false` vs `state=down`**: both halt data flow immediately and drain pending TX bytes into `stats/drops`. The difference is scope (`enabled` is per-device; `state` is per-bus) and reset behaviour (`state=reset` restores `enabled=1` on all devices; writing `enabled` only affects the target device). When `state=down` is set, `enabled` values are preserved unchanged and take effect again after `state=up`.

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

Writes take effect on the **next** open of `/dev/ttyVIRTLABx` (not live-resizable while open; deferred to a later revision).

> **TX vs RX overflow semantics**: the TX buffer (AUT → wire device) applies **backpressure** — it never evicts bytes; `write()` blocks or returns `-EAGAIN`. The RX buffer (wire device → AUT) applies **overflow-drop** — oldest byte evicted on full, `stats/overruns` incremented.

### Stats (read-only)

`/sys/kernel/virtrtlab/devices/uartN/stats/`

| Attribute | Type | Description |
|---|---|---|
| `tx_bytes` | u64 | Bytes received from the AUT, counted **before** fault injection. `tx_bytes − drops ≈ bytes actually delivered to the wire device`. |
| `rx_bytes` | u64 | Bytes received from wire device toward AUT |
| `overruns` | u64 | Bytes lost due to buffer saturation, in either direction: (a) **AUT → wire**: bytes that could not be enqueued into the wire-side RX fifo when it was full (TX path overflow); (b) **wire → AUT**: bytes that could not be inserted into the TTY flip buffer when it was full (RX path overflow). Incremented by the count of bytes lost per event. A high `overruns` value indicates a slow consumer on either side. |
| `drops` | u64 | Bytes that left the AUT TX buffer but were **not** delivered to the wire device, for any of the following reasons: (1) fault injection gate (`drop_rate_ppm`); (2) `state=down` with no daemon attached; (3) `enabled=false` gate — bytes already queued when `enabled` is written to `0`; (4) port close (`tty_close`) while TX bytes are still pending. Incremented by the byte count of the affected burst. Invariant: `tx_bytes − drops ≈ bytes actually delivered to the wire device`. |

Counters are reset by writing `0` to `stats/reset`. Counters wrap silently at `UINT64_MAX` (modular arithmetic, no saturation).

> **Counter units (UART)**: all four counters measure individual **bytes**. For future peripheral types (CAN, SPI, …), counter units are type-specific and documented in each peripheral's spec section; the common-attrs table does not define units.

> **`drops` vs `overruns`**: `drops` counts bytes lost on the **AUT → wire** TX path due to fault injection or bus/device state events. `overruns` counts bytes lost due to **buffer saturation** — either the wire-side RX fifo (AUT → wire, slow daemon) or the TTY flip buffer (wire → AUT, slow AUT reader). High `drops` signals fault injection activity; high `overruns` signals a slow consumer.

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

`virtrtlab_gpio` registers a native `gpio_chip` via `gpiochip_add_data()`, exposing
`/dev/gpiochipN` to the AUT. Fault injection is applied on the harness injection
path via the `inject` sysfs attr. The earlier custom `direction`, `value`,
`active_low`, `edge_rising`, and `edge_falling` sysfs attrs are **retired**.
No dependency on `gpio-sim` or any out-of-tree kernel patch.

### Overview

`virtrtlab_gpio` registers a native `gpio_chip` via `gpiochip_add_data()` on module
load. The chip is visible to the AUT via `/dev/gpiochipN` using the standard GPIO v2
character device API. Legacy AUTs using `/sys/class/gpio` remain supported through the
standard Linux GPIO sysfs ABI, with a dynamically assigned base number exported by
VirtRTLab. Fault injection (latency, jitter, drop, bitflip) is applied on every
harness-injected input transition, before the new line state reaches the AUT.

Three distinct surfaces exist:

- **AUT interface**: `/dev/gpiochipN` — standard GPIO v2 character device API
- **AUT interface (legacy)**: `/sys/class/gpio/gpio<base+L>/` — standard Linux sysfs GPIO ABI
- **Harness control plane**: `/sys/kernel/virtrtlab/devices/gpioN/` — `inject` attr for line-state injection, fault attrs, and stats

### Device provisioning

On module load, `virtrtlab_gpio` calls `gpiochip_add_data()` once per `gpioN` instance.
Each chip has 8 lines (fixed). The chip label in the gpiolib subsystem is
`"gpioN"` (e.g. `"gpio0"` for the first instance) — this is the device name assigned
by the `virtrtlab_bus` and returned by `gpio_device_get_label()`.

The gpiochip index `N` is assigned dynamically by gpiolib; the resulting `/dev/gpiochipN`
path is exposed via the `chip_path` sysfs attr so harness scripts can locate the correct
device without scanning all gpiochips.

The legacy sysfs GPIO base is assigned dynamically by gpiolib as well. VirtRTLab
exposes that base via the `sysfs_base` attr so legacy AUTs and harnesses can
derive line paths without probing unrelated gpiochips.

If the host kernel does not provide the legacy `/sys/class/gpio` ABI, `sysfs_base`
is absent and the GPIO bank remains usable through `/dev/gpiochipN` and the
VirtRTLab harness control plane.

`virtrtlab_gpio` declares `softdep: pre: virtrtlab_core` only. No dependency on
`gpio-sim` or any out-of-tree module.

### AUT interfaces

#### GPIO v2 character device API

The AUT may communicate with the simulated GPIO chip via the **GPIO v2 character
device API** on `/dev/gpiochipN`:

| AUT operation | ioctl / syscall | Description |
|---|---|---|
| Identify chip | `GPIO_GET_CHIPINFO_IOCTL` | Returns the host-assigned chip name, label `"gpioN"`, and line count |
| Request lines | `GPIO_V2_GET_LINE_IOCTL` | Allocate one or more lines with direction, edge-detection, bias, and active-low flags |
| Read line values | `GPIO_V2_LINE_GET_VALUES_IOCTL` | Read current logical value of requested lines |
| Drive output lines | `GPIO_V2_LINE_SET_VALUES_IOCTL` | Write output line state |
| Wait for edge events | `read()` on line fd | Block until a subscribed edge event arrives |
| Poll for events | `poll()` on line fd | `POLLIN` when an edge event is pending |

Line direction (input / output), edge-detection interest (rising / falling / both), bias,
and active-low polarity are **all set by the AUT at line-request time** via `ioctl`
flags. VirtRTLab does not expose `direction`, `active_low`, or edge masks in sysfs.

`ioctl(fd, GPIO_GET_CHIPINFO_IOCTL)` returns:
- `name`: the host-assigned gpiochip device name (for example `gpiochip4`)
- `label`: `"gpioN"` for the matching VirtRTLab instance
- `lines`: `num_lines` (default 8)

#### Legacy sysfs GPIO ABI

The AUT may also communicate with the simulated GPIO chip through the legacy
`/sys/class/gpio` ABI.

Mapping rule:

- `sysfs_base` is the first global GPIO number assigned to this `gpioN` bank
- line offset `L` (`0..7`) is exposed as `/sys/class/gpio/gpio<sysfs_base+L>/`

Example: if `sysfs_base` is `200`, line `3` of `gpio0` is exposed as
`/sys/class/gpio/gpio203/`.

VirtRTLab does not define a private sysfs ABI for AUT line access; it reuses the
standard Linux GPIO sysfs files (`direction`, `value`, `edge`, `active_low`) when
that ABI is enabled by the host kernel.

### Harness control plane — VirtRTLab sysfs

`/sys/kernel/virtrtlab/devices/gpioN/`

#### Identity attrs

| Attribute | Access | Type | Description |
|---|---|---|---|
| `type` | ro | string | `"gpio"` |
| `bus` | ro | string | Parent bus, e.g. `vrtlbus0` |
| `num_lines` | ro | u8 | Number of lines on this chip. Fixed at `8` per instance. |
| `chip_path` | ro | string | Absolute path to the AUT-facing character device, e.g. `/dev/gpiochip2`. Derived from the registered gpiochip device name at `gpiochip_add_data()` time. |
| `sysfs_base` | ro | u32 | First global GPIO number assigned to this bank for the legacy `/sys/class/gpio` ABI. Line `L` maps to `gpio<sysfs_base+L>`. Attribute may be absent when the host kernel disables the legacy ABI. |
| `inject` | wo | string | `"N:V"` — inject value `V` (`0` or `1`) on input line `N` (`0`..`7`). Triggers the 7-step fault injection shim. Writes to AUT-owned output lines are silently ignored at commit time (step 6). `read()` returns `-EPERM`. |

#### Fault attrs

Semantics identical to the common fault attrs defined above, adapted for GPIO:

| Attribute | Access | Type | Default | Constraints |
|---|---|---|---|---|
| `enabled` | rw | bool | `1` | Device-level gate. `0` disables fault injection (shim passes all harness writes through immediately with no latency, drop, or bitflip). Does **not** affect the AUT-facing `/dev/gpiochipN` device. |
| `latency_ns` | rw | u64 | `0` | Delay between harness injection write and AUT line-state update (ns). |
| `jitter_ns` | rw | u64 | `0` | Uniform jitter amplitude (ns) added to `latency_ns` per transition; sampled as a uniform random value in $[0, \text{jitter\_ns}]$. |
| `drop_rate_ppm` | rw | u32 | `0` | Probability of suppressing a harness-injected line transition (parts per million, per transfer unit). |
| `bitflip_rate_ppm` | rw | u32 | `0` | Probability of inverting a harness-injected line value before delivery to the AUT (parts per million, per transfer unit). |

### Harness injection path

The harness drives **input lines** by writing to the `inject` sysfs attr:

```
/sys/kernel/virtrtlab/devices/gpioN/inject
```

Format: `"N:V"` where `N` is the zero-based line index (`0`..`7`) and `V` is the
physical value (`0` or `1`). Both fields must be decimal integers; any other form returns
`-EINVAL`.

Values are in the **physical** domain: the AUT's per-line active-low flag (set via
`GPIO_V2_GET_LINE_IOCTL`) affects how the AUT reads the value, not how the harness
writes it. A harness writing `inject = "0:1"` injects a physical high on line 0; if the
AUT requested that line with `GPIO_V2_LINE_FLAG_ACTIVE_LOW`, it reads the value as
logical low.

### Fault injection shim — observable behaviour

When the harness writes to the `inject` attr, the shim executes the following sequence
before the new state reaches the AUT:

1. **Snapshot** — the shim captures the current `latency_ns`, `jitter_ns`,
   `drop_rate_ppm`, `bitflip_rate_ppm`, and requested line value at the moment the `inject`
   attr write is processed. Later sysfs writes to fault attrs do not retroactively modify a
   transition already in the shim pipeline.
2. **`enabled` check** — if `enabled=0` the injection is silently discarded: the
   `inject` write returns success (`count`) but no value is committed, no fault gate
   is evaluated, and no counter is updated. The line state is unchanged.
3. **Drop gate** — one PRNG draw is taken. If `drop_rate_ppm` fires, the transition is
   suppressed: the line value is rolled back to its previous state, `stats/drops` is
   incremented, and the sequence terminates — no AUT notification is generated.
4. **Bitflip gate** — one PRNG draw is taken. If `bitflip_rate_ppm` fires, the delivered
   value is inverted (physical domain). `stats/bitflips` is incremented.
5. **Latency scheduling** — the shim schedules delivery after
   `latency_ns + uniform_random(0, jitter_ns)` nanoseconds via a per-line hrtimer.
6. **Commit** — on timer expiry, the (possibly bitflipped) value is committed to the
   AUT-facing line state. `stats/value_changes` is incremented if and only if the new
   state differs from the previous state. For lines currently owned by the AUT as output,
   the commit is silently skipped (AUT remains authoritative) but `stats/bitflips` is
   still updated if the bitflip gate fired.
7. **No-transition case** — if the committed value equals the previous state (e.g. a
   bitflip caused a round-trip back to the original value), `stats/value_changes` does
   **not** increment for that line.

PRNG draws use the bus-level xorshift32 PRNG (`buses/vrtlbus0/seed`). Draw order across
devices on the same bus is the same interleaved order as for UART.

### Transfer unit

For GPIO, one transfer unit = **one write to the `inject` sysfs attr**. The transfer unit
is evaluated once per write, independent of whether the requested
value matches the current line state (though a no-change write may resolve to zero
`stats/value_changes` increments at commit time).

The `drop_rate_ppm` and `bitflip_rate_ppm` decisions are taken **per line per write** —
if a future API allows multi-line atomic writes, each line is evaluated independently.

### Stats

`/sys/kernel/virtrtlab/devices/gpioN/stats/`

| Attribute | Type | Description |
|---|---|---|
| `value_changes` | u64 | Count of line transitions actually committed to the AUT-facing line state (input lines only). Increments only when the new state differs from the previous state. |
| `drops` | u64 | Count of harness-injected transitions suppressed by `drop_rate_ppm`. One increment per suppressed write (per transfer unit). |
| `bitflips` | u64 | Count of bitflip gate fires: increments whenever `bitflip_rate_ppm` inverts the injected value, regardless of line direction. |
| `reset` | wo | Writing `0` resets all GPIO stats counters atomically. Any other value returns `-EINVAL`. `read()` returns `-EPERM`. |

Counters wrap silently at `UINT64_MAX` (modular arithmetic, no saturation). Bus
`state=reset` resets all GPIO stats counters and resets fault attrs to `0`/`1` (same as
UART); it does **not** affect line state or the AUT's open line descriptors.

### Kernel configuration requirements

| Kconfig symbol | Required | Notes |
|---|---|---|
| `CONFIG_GPIOLIB` | mandatory | Core gpiolib; enables `gpiochip_add_data()` and the GPIO v2 ioctl API |
| `CONFIG_GPIO_CDEV` | mandatory | `/dev/gpiochipN` character device; enabled by default when `CONFIG_GPIOLIB=y` (Linux ≥ 5.10) |

### Error behaviour

| Condition | Behaviour |
|---|---|
| `inject` write with `N` out of range (`N` > 7) | return `-EINVAL` |
| `inject` write with `V` not in `{0, 1}` | return `-EINVAL` |
| `inject` write with malformed format (missing `:`, non-decimal, empty) | return `-EINVAL` |
| `read()` on `inject` | permission error (write-only attr, mode `0200`) |
| `inject` write while `enabled=0` | return `count` (success); injection silently discarded, line state unchanged |
| `inject` write while bus `state=down` | return `-EIO` |
| `inject` write on line currently owned by AUT as output | write returns `count` (success); the injection is accepted, goes through drop/bitflip/latency stages, but commits to the stored line value only — the AUT's `GPIO_V2_LINE_SET_VALUES_IOCTL` remains authoritative for the output state |
| `read()` on `stats/reset` | return `-EPERM` |
| `stats/reset` write value other than `0` | return `-EINVAL` |
| `latency_ns`/`jitter_ns` write > 10 000 000 000 ns | return `-EINVAL` |
| `drop_rate_ppm`/`bitflip_rate_ppm` write > 1 000 000 | return `-EINVAL` |
| AUT `open("/dev/gpiochipN")` | always succeeds regardless of `enabled` or bus `state` |
| `rmmod virtrtlab_gpio` with AUT holding open line fds | gpiochip is removed; open line fds return `-ENODEV` on subsequent ioctl |

### Test-oriented examples

Discover the chip path and drive line 0 high via the `inject` attr:

```sh
CHIP=$(cat /sys/kernel/virtrtlab/devices/gpio0/chip_path)  # e.g. /dev/gpiochip2
echo 0:1 > /sys/kernel/virtrtlab/devices/gpio0/inject
```

Verify the AUT can read the injected value using `gpioget` (libgpiod ≥ 1.6):

```sh
gpioget --chip $CHIP 0
# Expected output: 1
```

Enable drop-all and verify transitions are suppressed:

```sh
echo 1000000 > /sys/kernel/virtrtlab/devices/gpio0/drop_rate_ppm
echo 0:1 > /sys/kernel/virtrtlab/devices/gpio0/inject
gpioget --chip $CHIP 0
# Expected output: 0  (line state unchanged)
cat /sys/kernel/virtrtlab/devices/gpio0/stats/drops
# Expected output: 1
```

Verify edge event delivery with latency:

```sh
echo 5000000 > /sys/kernel/virtrtlab/devices/gpio0/latency_ns  # 5 ms
gpionotify --chip $CHIP --rising-edge 0 &
NOTIFY_PID=$!
echo 0:1 > /sys/kernel/virtrtlab/devices/gpio0/inject
# Edge event arrives ~5 ms after the write above
wait $NOTIFY_PID
```

### Decisions

**Open 1 — Harness injection surface** → **closed**: `inject` sysfs attr at
`/sys/kernel/virtrtlab/devices/gpioN/inject`, format `"N:V"`. Provides a stable,
version-controlled injection path; no dependency on gpio-sim debugfs layout.

**Open 2 — Shim hook feasibility** → **closed**: `virtrtlab_gpio` registers a native
`gpio_chip` via `gpiochip_add_data()`. Fault injection is applied inside `inject_store()`
before committing the line value via `gpiochip_set_value_cansleep()`. All code uses
public gpiolib APIs only; no in-tree patch required.

**Open 3 — Stable `/dev` path** → **closed**: `chip_path` sysfs attr is the primary
discovery mechanism. Derived from the registered gpiochip device name at
`gpiochip_add_data()` time and exposed read-only at
`/sys/kernel/virtrtlab/devices/gpioN/chip_path`. No udev symlink required.

**Open 4 — `bus state=down` semantics for GPIO** → **deferred**: `state=down`
is not propagated to the GPIO device in the current spec. `inject` writes while bus is `down`
return `-EIO` (bus gate enforced in `inject_store()`). The AUT's open line fds are
unaffected by bus state.

**Open 5 — `num_lines` configurability** → **closed**: fixed at 8 per instance in
the current spec. Exposed read-only via the `num_lines` sysfs attr. Configurable bank widths
are deferred to a later revision.

**Open 6 — `active_low` read-back** → **closed (not exposed)**: the harness writes
physical values; the AUT controls polarity via its `GPIO_V2_GET_LINE_IOCTL` flags.
Exposing `active_low` from gpiolib internal state would require non-public API access and
is not needed in the current spec.

## Rationale

**Why are baud/parity/databits/stopbits read-only?**  
The AUT configures the serial line via `tcsetattr()` — this is the standard POSIX API. VirtRTLab mirrors the termios state in sysfs so user scripts and the daemon can observe it, but the AUT remains the sole authority. Allowing writes would create a split-brain scenario.

**Why no `mode` or `fault_policy` in sysfs?**  
Record/replay and named fault profiles are orchestration concepts. They are cleaner to implement in Python scripts that write individual sysfs attrs, rather than encoding policy state in the kernel. This keeps the kernel surface minimal and auditable.

**Why use a native `gpio_chip`?**
The earlier custom banked GPIO model provided no standard AUT interface: a userspace AUT using `libgpiod` or the GPIO v2 ioctl API could not interact with it. VirtRTLab's purpose is to simulate real hardware the AUT has been compiled against; a private sysfs-only bank model defeats this goal. Registering a native `struct gpio_chip` via `gpiochip_add_data()` exposes a standards-compliant `/dev/gpiochipN` char device using only public kernel API — no out-of-tree dependency, no `CONFIG_GPIO_SIM`, no configfs/debugfs bootstrap required.

**Why keep legacy `/sys/class/gpio` support as well?**
Some existing AUTs still use the legacy sysfs GPIO ABI. VirtRTLab cannot require those adopters to rewrite their GPIO stack solely to run CI against the simulator. The stable contract is therefore dual-surface: GPIO chardev for modern users, `/sys/class/gpio` for legacy users, and VirtRTLab sysfs only for harness control.

**Why keep VirtRTLab sysfs for fault control?**
Fault injection parameters are dynamic: CI test scripts change them at runtime between test cases. The native `gpio_chip` API has no built-in runtime harness surface. VirtRTLab sysfs attrs (`inject`, `latency_ns`, `drop_rate_ppm`, etc.) remain the correct layer for runtime harness control.

## Decisions

**Buffer live-resize** — deferred to a later revision: `tx_buf_sz`/`rx_buf_sz` writes rejected while device is open (`-EBUSY`).

**Baud rate change notification** — not in v0.1.0: `tcsetattr()` updates termios state and the sysfs `baud` attr atomically. `virtrtlabd` reads `baud` from sysfs on demand; no uevent or control byte is generated by the kernel.

**PRNG scope** — the xorshift32 state lives at the bus level (`buses/vrtlbus0/seed`), shared across all devices on that bus. Devices on the same bus draw from the shared state in interleaved order; each device does not maintain its own PRNG. For reproducible CI results, write `seed` before activating stochastic fault injection and record it in test artifacts.

**PRNG lifecycle** — the PRNG state is initialised to `1` at module load and is **not** reset by `state=reset`. This is intentional: `state=reset` resets the fault _parameters_ (rates, latency, etc.) but not the _sequence_ from which draws are taken. Two consequences:
- After `rmmod`/`insmod`, the fault injection sequence is identical to the previous load if `seed` is not written — deterministic by default.
- After `state=reset`, the next fault draw continues from wherever the PRNG left off. If test reproducibility requires a known sequence across resets, write `seed` explicitly after each `state=reset`.

**GPIO line count** — fixed at 8 lines per `gpioN` instance. `num_lines` is read-only in sysfs and reflects this fixed value. Configurability is deferred to a later revision.

**GPIO transfer unit** — defined as one write to `inject` targeting a single line, aligning with the GPIO v2 API's per-line granularity and the `inject` attr's `"N:V"` format. `drop_rate_ppm` and `bitflip_rate_ppm` are evaluated independently for each line write.

**`stats/bitflips` counter** — tracked separately from `stats/value_changes` so test assertions can distinguish intended transitions (AUT response to correct injection) from corruption events (bitflip gate fired).
