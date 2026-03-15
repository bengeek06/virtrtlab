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

> **Transfer unit definition (v0.1.0)**: transfer-unit granularity is peripheral-specific. For UART, one transfer unit is one byte paced by the hrtimer. For GPIO, one transfer unit is one sysfs write to `value`, potentially affecting multiple bits of the same bank.

> **`latency_ns` / `jitter_ns` semantics**: delivery timing is peripheral-specific. For UART, these attrs delay byte delivery toward the wire device and therefore influence TX backpressure. For GPIO, they delay sysfs-injected input transitions before the logical line state changes; see the GPIO section below.

> **Fault attr update timing**: writes to `latency_ns`, `jitter_ns`, `drop_rate_ppm`, or `bitflip_rate_ppm` take effect from the **next transfer scheduling point** after the sysfs store returns. For UART, that means the next hrtimer callback; for GPIO, the next sysfs write to `value`. No already-scheduled transfer unit is modified in place.

> **GPIO delayed-write snapshot rule**: when a GPIO `value` write is accepted for delayed delivery, the kernel snapshots the requested logical bank value together with the current `latency_ns`, `jitter_ns`, `drop_rate_ppm`, `bitflip_rate_ppm`, `direction`, and `active_low` state before the sysfs store returns. A later change to those attrs does not rewrite a bank update that was already accepted and scheduled.

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

Writes take effect on the **next** open of `/dev/ttyVIRTLABx` (not live-resizable while open; deferred to v0.2.0).

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

> **v0.2.0** — replaces the v0.1.0 custom banked GPIO model. `virtrtlab_gpio` now acts
> as a fault-injection overlay on top of the Linux `gpio-sim` driver. The custom
> `direction`, `value`, `active_low`, `edge_rising`, and `edge_falling` sysfs attrs are
> **retired**. The AUT now communicates via the standard GPIO v2 character device API.

### Overview

`virtrtlab_gpio` is a **thin fault-injection shim** layered on top of the Linux
`gpio-sim` driver (Linux ≥ 5.17, `CONFIG_GPIO_SIM`). `gpio-sim` creates a
fully-standard gpiochip visible to the AUT via `/dev/gpiochipN`. VirtRTLab registers a
shim on that chip's data path to intercept harness-injected line transitions and apply
configurable fault injection (latency, jitter, drop, bitflip) before the transition
reaches the AUT.

Two distinct control planes exist:

- **AUT interface**: `/dev/gpiochipN` — standard GPIO v2 character device API
- **Harness control plane**: `/sys/kernel/virtrtlab/devices/gpioN/` — fault attrs and
  stats only (no direct line-state writes)
- **Harness injection path**: gpio-sim debugfs — the harness drives input line state
  here; VirtRTLab intercepts and applies fault injection

### Device provisioning

On module load, `virtrtlab_gpio` provisions one `gpio-sim` chip per `gpioN` instance.
Each chip has `num_lines` lines (default: 8). The chip label in the gpiolib subsystem is
`"virtrtlab-gpioN"` (e.g. `"virtrtlab-gpio0"` for the first instance).

The gpiochip appears at `/dev/gpiochipN` where `N` is assigned dynamically by the kernel
gpiolib. The mapping from VirtRTLab instance name to `/dev` path is exposed via the
`chip_path` sysfs attr (see table below).

`virtrtlab_gpio` declares `softdep: pre: gpio-sim`. Loading `virtrtlab_gpio.ko` when
`gpio-sim` is absent returns `-ENODEV`; the module does not remain resident.

> **Open 5:** Should `num_lines` be a per-bank sysfs attr settable before module load, a
> module parameter, or fixed at 8?

### AUT interface

The AUT communicates with the simulated GPIO chip exclusively via the **GPIO v2
character device API** on `/dev/gpiochipN`:

| AUT operation | ioctl / syscall | Description |
|---|---|---|
| Identify chip | `GPIO_GET_CHIPINFO_IOCTL` | Returns label `"virtrtlab-gpio0"`, name, and line count |
| Request lines | `GPIO_V2_GET_LINE_IOCTL` | Allocate one or more lines with direction, edge-detection, bias, and active-low flags |
| Read line values | `GPIO_V2_LINE_GET_VALUES_IOCTL` | Read current logical value of requested lines |
| Drive output lines | `GPIO_V2_LINE_SET_VALUES_IOCTL` | Write output line state |
| Wait for edge events | `read()` on line fd | Block until a subscribed edge event arrives |
| Poll for events | `poll()` on line fd | `POLLIN` when an edge event is pending |

Line direction (input / output), edge-detection interest (rising / falling / both), bias,
and active-low polarity are **all set by the AUT at line-request time** via `ioctl`
flags. VirtRTLab does not expose `direction`, `active_low`, or edge masks in sysfs.

`ioctl(fd, GPIO_GET_CHIPINFO_IOCTL)` returns:
- `name`: `"virtrtlab-gpio0"` (or `"virtrtlab-gpioN"`)
- `label`: same as name
- `lines`: `num_lines` (default 8)

### Harness control plane — VirtRTLab sysfs

`/sys/kernel/virtrtlab/devices/gpioN/`

#### Identity attrs

| Attribute | Access | Type | Description |
|---|---|---|---|
| `type` | ro | string | `"gpio"` |
| `bus` | ro | string | Parent bus, e.g. `vrtlbus0` |
| `num_lines` | ro | u8 | Number of lines provisioned in the gpio-sim chip (default: `8`) |
| `chip_path` | ro | string | Absolute path to the AUT-facing character device, e.g. `/dev/gpiochip2`. Allows harness scripts to locate the correct `/dev` node without scanning all gpiochips. |

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

The harness drives **input lines** by writing to the gpio-sim debugfs interface:

```
/sys/kernel/debug/gpio-sim/<chip-label>/<lineN>/value
```

where `<chip-label>` matches the `chip_path` attr basename (e.g.
`/sys/kernel/debug/gpio-sim/virtrtlab-gpio0/`), and `<lineN>` is the debugfs line name
assigned by gpio-sim (e.g. `line0`, `line1`, …, `line7`).

Write `0` for logical low, `1` for logical high. Values are in the **physical** domain:
gpio-sim does not apply active-low inversion at the debugfs layer. The AUT's per-line
active-low flag (set via `GPIO_V2_GET_LINE_IOCTL`) affects how the AUT reads the value,
not how the harness writes it.

> **Open 1:** Should VirtRTLab expose a `value` sysfs attr at
> `/sys/kernel/virtrtlab/devices/gpioN/value` as a stable per-bank (or per-line) harness
> injection surface that internally wraps the gpio-sim debugfs write? This would preserve
> harness script compatibility with v0.1.0 and decouple harness scripts from the
> debugfs path structure.

> **Open 6:** Should VirtRTLab expose a read-only `active_low` attr derived from gpio-sim
> internal state, so harness scripts can compute the correct physical injection value for a
> given AUT-requested logical polarity?

### Fault injection shim — observable behaviour

When the harness writes a value to a gpio-sim debugfs line file, the shim executes the
following sequence before the new state reaches the AUT:

1. **Snapshot** — the shim captures the current `latency_ns`, `jitter_ns`,
   `drop_rate_ppm`, `bitflip_rate_ppm`, and requested line value at the moment the debugfs
   write is processed. Later sysfs writes to fault attrs do not retroactively modify a
   transition already in the shim pipeline.
2. **`enabled` check** — if `enabled=0` the shim is bypassed: the written value is
   committed immediately to the AUT-facing line state, no fault evaluation occurs, no
   counter is updated.
3. **Drop gate** — one PRNG draw is taken. If `drop_rate_ppm` fires, the transition is
   suppressed: the line value is rolled back to its previous state, `stats/drops` is
   incremented, and the sequence terminates — no AUT notification is generated.
4. **Bitflip gate** — one PRNG draw is taken. If `bitflip_rate_ppm` fires, the delivered
   value is inverted (physical domain). `stats/bitflips` is incremented.
5. **Latency scheduling** — the shim schedules delivery after
   `latency_ns + uniform_random(0, jitter_ns)` nanoseconds via a per-line hrtimer.
6. **Commit** — on timer expiry, the (possibly bitflipped) value is committed to the
   AUT-facing line state. If the AUT has subscribed to edge events for this line and a
   matching transition occurred:
   - an edge event is enqueued in the AUT's line fd
   - `stats/edge_events` is incremented by 1
   - `stats/value_changes` is incremented by 1 for each line whose logical state changed
     (from the AUT's perspective, after its own active-low flag is applied)
7. **No-transition case** — if the committed value equals the previous state (e.g. a
   bitflip caused a round-trip back to the original value), `stats/value_changes` does
   **not** increment for that line.

PRNG draws use the bus-level xorshift32 PRNG (`buses/vrtlbus0/seed`). Draw order across
devices on the same bus is the same interleaved order as for UART.

> **Open 2:** Is the shim notification hook (step 1 above) implementable using only
> public gpiolib APIs (`gpio_chip` ops wrapper or `irq_chip` shim), or does it require
> modifications to `gpio-sim.c`? The answer bounds what can be deterministically specified
> here. If the hook requires in-tree changes, this spec is subject to revision once the
> implementation boundary is confirmed.

### Transfer unit (v0.2.0)

For GPIO, one transfer unit = **one harness write to a single gpio-sim debugfs line value
file**. The transfer unit is evaluated once per write, independent of whether the requested
value matches the current line state (though a no-change write may resolve to zero
`stats/value_changes` increments at commit time).

The `drop_rate_ppm` and `bitflip_rate_ppm` decisions are taken **per line per write** —
if a future API allows multi-line atomic writes, each line is evaluated independently.

### Stats

`/sys/kernel/virtrtlab/devices/gpioN/stats/`

| Attribute | Type | Description |
|---|---|---|
| `value_changes` | u64 | Count of line transitions actually applied to the AUT-facing line state after fault handling and latency timer expiry. One increment per affected line per committed write. |
| `edge_events` | u64 | Count of edge events enqueued in AUT line fds as a result of applied transitions, summed over all subscribed lines. Only rising/falling events matching the AUT's ioctl-registered edge interest are counted. |
| `drops` | u64 | Count of harness-injected line transitions suppressed by `drop_rate_ppm`. One increment per suppressed write (per transfer unit). |
| `bitflips` | u64 | Count of harness-injected line transitions whose value was inverted by `bitflip_rate_ppm` before delivery. New in v0.2.0. |
| `reset` | wo | Writing `0` resets all GPIO stats counters atomically. Any other value returns `-EINVAL`. `read()` returns `-EPERM`. |

Counters wrap silently at `UINT64_MAX` (modular arithmetic, no saturation). Bus
`state=reset` resets all GPIO stats counters and resets fault attrs to `0`/`1` (same as
UART); it does **not** affect line state or the AUT's open line descriptors.

### Kernel configuration requirements

| Kconfig symbol | Required | Notes |
|---|---|---|
| `CONFIG_GPIO_SIM` | mandatory | gpio-sim driver (Linux ≥ 5.17) |
| `CONFIG_GPIO_CDEV` | mandatory | `/dev/gpiochipN` character device; enabled by default when `CONFIG_GPIOLIB=y` (Linux ≥ 5.10) |
| `CONFIG_GPIOLIB` | mandatory | Core gpiolib |
| `CONFIG_CONFIGFS_FS` | mandatory | Required by gpio-sim chip provisioning (module load) |
| `CONFIG_DEBUG_FS` | mandatory | Required for the harness injection path (`gpio-sim` debugfs) |

### Error behaviour

| Condition | Behaviour |
|---|---|
| `insmod virtrtlab_gpio.ko` with `gpio-sim` not loaded | return `-ENODEV`; module does not remain resident |
| `latency_ns`/`jitter_ns` write > 10 000 000 000 ns | return `-EINVAL` |
| `drop_rate_ppm`/`bitflip_rate_ppm` write > 1 000 000 | return `-EINVAL` |
| `stats/reset` write value other than `0` | return `-EINVAL` |
| `read()` on `stats/reset` | return `-EPERM` |
| Harness writes to gpio-sim debugfs while `enabled=0` | passed through without fault injection; no error returned |
| AUT `open("/dev/gpiochipN")` | always succeeds regardless of `enabled` or bus `state` |
| AUT requests a line as output; harness writes to that line's debugfs value | write succeeds; gpio-sim ignores harness writes on AUT-owned output lines |
| `rmmod virtrtlab_gpio` with AUT holding open line fds | gpio-sim chip is removed; open line fds return `-ENODEV` on subsequent ioctl |

> **Open 3:** What stable path does the harness use to locate `/dev/gpiochipN`? The
> `chip_path` attr is proposed as the primary mechanism. An alternative is a udev rule
> that creates a symlink at `/dev/virtrtlab-gpio0`. Decision needed before v0.2.0 is
> tagged.

> **Open 4:** Does `bus state=down` block or pass-through harness GPIO injection? For
> UART, `state=down` halts the daemon path. For GPIO, gpio-sim is a separate subsystem and
> `state=down` does not automatically propagate to it. Aligning semantics across device
> families is desirable but requires an explicit decision.

### Test-oriented examples

Discover the chip path and drive line 0 high:

```sh
CHIP=$(cat /sys/kernel/virtrtlab/devices/gpio0/chip_path)  # e.g. /dev/gpiochip2
CHIP_LABEL=$(gpiodetect | awk -v chip="$CHIP" '$0 ~ chip { print $2 }')  # e.g. virtrtlab-gpio0
echo 1 > /sys/kernel/debug/gpio-sim/${CHIP_LABEL}/line0/value
```

Verify the AUT can read the injected value using `gpioget` (libgpiod ≥ 1.6):

```sh
gpioget --chip $CHIP 0
# Expected output: 1
```

Enable drop-all and verify transitions are suppressed:

```sh
echo 1000000 > /sys/kernel/virtrtlab/devices/gpio0/drop_rate_ppm
echo 1 > /sys/kernel/debug/gpio-sim/virtrtlab-gpio0/line0/value
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
echo 1 > /sys/kernel/debug/gpio-sim/virtrtlab-gpio0/line0/value
# Edge event arrives ~5 ms after the write above
wait $NOTIFY_PID
cat /sys/kernel/virtrtlab/devices/gpio0/stats/edge_events
# Expected output: 1
```

### Open questions

> **Open 1:** Stable harness injection surface — should VirtRTLab expose a `value` or
> `inject` sysfs attr that wraps the gpio-sim debugfs write, for harness script
> compatibility with v0.1.0?

> **Open 2:** Shim hook feasibility — is the fault-injection notification hook
> implementable via public gpiolib APIs, or does it require in-tree modification to
> `gpio-sim.c`?

> **Open 3:** Stable `/dev` path — `chip_path` sysfs attr vs. udev symlink at
> `/dev/virtrtlab-gpioN`.

> **Open 4:** `bus state=down` semantics for GPIO — block injection or pass-through?

> **Open 5:** `num_lines` configurability — module parameter, per-bank sysfs attr, or
> fixed at 8.

> **Open 6:** `active_low` read-back — should VirtRTLab expose a read-only per-line
> `active_low` attr derived from the AUT's ioctl flags, to help harness scripts compute
> the correct injection value?

## Rationale

**Why are baud/parity/databits/stopbits read-only?**  
The AUT configures the serial line via `tcsetattr()` — this is the standard POSIX API. VirtRTLab mirrors the termios state in sysfs so user scripts and the daemon can observe it, but the AUT remains the sole authority. Allowing writes would create a split-brain scenario.

**Why no `mode` or `fault_policy` in sysfs?**  
Record/replay and named fault profiles are orchestration concepts. They are cleaner to implement in Python scripts that write individual sysfs attrs, rather than encoding policy state in the kernel. This keeps the kernel surface minimal and auditable.

**Why retire the custom banked GPIO and adopt gpio-sim (v0.2.0)?**  
The v0.1.0 custom gpiochip implementation provided no standard AUT interface: a userspace AUT using `libgpiod` or the GPIO v2 ioctl API could not interact with it. VirtRTLab's purpose is to simulate real hardware the AUT has been compiled against; a private sysfs-only bank model defeats this goal. `gpio-sim` is upstream, maintained, and provides both the char-device AUT interface and a debugfs harness injection surface, eliminating the need to re-implement gpiolib chip infrastructure.

**Why keep VirtRTLab sysfs for fault control rather than gpio-sim configfs (v0.2.0)?**  
Fault injection parameters are dynamic: CI test scripts change them at runtime between cases. gpio-sim's configfs surface is a provisioning-time interface (used at chip creation), not a runtime control surface. VirtRTLab sysfs remains the correct layer for runtime harness control.

## Decisions

**Buffer live-resize** — deferred to v0.2.0: `tx_buf_sz`/`rx_buf_sz` writes rejected while device is open (`-EBUSY`).

**Baud rate change notification** — not in v0.1.0: `tcsetattr()` updates termios state and the sysfs `baud` attr atomically. `virtrtlabd` reads `baud` from sysfs on demand; no uevent or control byte is generated by the kernel.

**PRNG scope** — the xorshift32 state lives at the bus level (`buses/vrtlbus0/seed`), shared across all devices on that bus. Devices on the same bus draw from the shared state in interleaved order; each device does not maintain its own PRNG. For reproducible CI results, write `seed` before activating stochastic fault injection and record it in test artifacts.

**PRNG lifecycle** — the PRNG state is initialised to `1` at module load and is **not** reset by `state=reset`. This is intentional: `state=reset` resets the fault _parameters_ (rates, latency, etc.) but not the _sequence_ from which draws are taken. Two consequences:
- After `rmmod`/`insmod`, the fault injection sequence is identical to the previous load if `seed` is not written — deterministic by default.
- After `state=reset`, the next fault draw continues from wherever the PRNG left off. If test reproducibility requires a known sequence across resets, write `seed` explicitly after each `state=reset`.

**GPIO line count** — default 8 lines per `gpioN` instance in v0.2.0. The exact configurability surface (module parameter vs. sysfs attr) is an open question (Open 5); `num_lines` is read-only in sysfs and reflects the value chosen at provisioning time.

**GPIO transfer unit** — changed from bank-write (v0.1.0) to per-line-write (v0.2.0) to align with gpio-sim's per-line debugfs interface and the GPIO v2 API's per-line granularity. `drop_rate_ppm` and `bitflip_rate_ppm` are now evaluated independently for each line write.

**`stats/bitflips` counter** — added in v0.2.0. In v0.1.0, bitflips were subsumed in `stats/value_changes`. Separating them allows test assertions to distinguish intended transitions (AUT response to correct injection) from corruption events (bitflip gate fired).
