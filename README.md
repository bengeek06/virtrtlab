# VirtRTLab

VirtRTLab is a Linux/POSIX real‑time testing framework based on **kernel modules** that simulate common peripherals (UART, CAN, SPI, DAC, ADC, …) on a **virtual bus**.

Goal: run an application-under-test (AUT) *as if it were connected to real hardware*, while VirtRTLab provides:

- Peripheral discovery/connection via sysfs
- Runtime configuration via sysfs
- A control/injection socket to introduce **faults, jitter, delays, drops, bit flips**, etc.
- CI-oriented scenarios that help surface **race conditions, deadlocks, priority inversion, starvation**, and other timing-sensitive bugs

This document defines **v1 naming and interface conventions** so modules and tooling stay consistent.

---

## 1) Naming conventions

### Project and prefixes

- Project name: **VirtRTLab**
- Kernel prefix (symbols, modules, Kconfig): `VIRTRTLAB` / `virtrtlab_…`
- Sysfs namespace: `virtrtlab`
- Userspace control naming:
  - CLI: `virtrtlabctl`
  - Socket: `/run/virtrtlab.sock` (UNIX domain socket)
  - State dir: `/run/virtrtlab/`

### Module names

One core module + multiple peripheral modules:

- Core:
  - `virtrtlab_core` (virtual bus + common infra)
- Peripherals:
  - `virtrtlab_uart`
  - `virtrtlab_can`
  - `virtrtlab_spi`
  - `virtrtlab_adc`
  - `virtrtlab_dac`
  - (future) `virtrtlab_i2c`, `virtrtlab_gpio`, …

### Object naming

- Bus instance: `vrtlbus<N>` (e.g. `vrtlbus0`)
- Device instance: `<type><N>` (e.g. `uart0`, `can1`, `spi0`, `adc0`, `dac0`)
- Endpoint/port naming (when applicable): `port<N>`

---

## 2) Architecture (v1)

VirtRTLab is split into:

1. **Kernel side**
   - `virtrtlab_core`: provides a virtual bus and a common "fault/jitter engine".
   - Peripheral modules register devices on the bus and expose configuration through sysfs.
   - A kernel control plane receives injection commands and applies them deterministically.

2. **Userspace side**
   - `virtrtlabctl`: config helper (discover devices, apply profiles, inject faults).
   - A UNIX socket server (could be inside `virtrtlabctl` or a small daemon) that:
     - accepts injection commands
     - forwards them to the kernel module (e.g., via netlink, ioctl on a control char dev, or a misc device)

> Implementation detail (netlink vs misc device vs ioctl) is left open, but the **socket API** below is fixed for tooling.

---

## 3) sysfs layout

VirtRTLab exposes a stable sysfs API. The recommended layout is under `/sys/kernel/virtrtlab/` (kobject-based) to keep it decoupled from any specific subsystem.

### Root

`/sys/kernel/virtrtlab/`

- `version` (ro): semantic version string, e.g. `0.1.0`
- `build_id` (ro): git SHA/date
- `buses/`
- `devices/`
- `profiles/` (optional; may be userspace-managed)
- `stats/` (global counters)

### Bus instances

`/sys/kernel/virtrtlab/buses/vrtlbus0/`

- `state` (rw): `up|down|reset`
- `clock_ns` (ro): monotonic timestamp sampled by core
- `seed` (rw): RNG seed for stochastic profiles
- `default_policy` (rw): default injection policy name

### Devices

`/sys/kernel/virtrtlab/devices/uart0/`

Common files (all device types):

- `type` (ro): `uart|can|spi|adc|dac|…`
- `bus` (ro): `vrtlbus0`
- `enabled` (rw): `0|1`
- `mode` (rw): `normal|record|replay`
- `latency_ns` (rw): base latency added to operations
- `jitter_ns` (rw): uniform jitter amplitude
- `drop_rate_ppm` (rw): drops per million
- `bitflip_rate_ppm` (rw): bit flips per million
- `fault_policy` (rw): active policy name
- `stats/` (ro): per-device counters
  - `tx_frames`, `rx_frames`, `drops`, `timeouts`, `crc_errors`, … (type-specific allowed)

Type-specific examples:

- UART (`/sys/kernel/virtrtlab/devices/uart0/`)
  - `baud` (rw)
  - `parity` (rw): `none|even|odd`
  - `databits` (rw): `5|6|7|8`
  - `stopbits` (rw): `1|2`

- CAN (`…/can0/`)
  - `bitrate` (rw)
  - `fd_enabled` (rw): `0|1`
  - `arb_loss_rate_ppm` (rw)

- SPI (`…/spi0/`)
  - `mode` (rw): `0|1|2|3`
  - `max_hz` (rw)

- ADC (`…/adc0/`)
  - `channels` (ro)
  - `sample_rate_hz` (rw)
  - `noise_uV_rms` (rw)

- DAC (`…/dac0/`)
  - `channels` (ro)
  - `slew_limit_uV_per_us` (rw)

---

## 4) Control & injection socket

### Socket path and transport

- UNIX domain socket: `/run/virtrtlab.sock`
- Protocol: **line-delimited JSON** (one JSON object per line)
  - easy to test with `socat`/`nc`
  - robust for CI and for sending batches

### Message envelope (v1)

All messages follow:

```json
{
  "id": "uuid-or-ci-step-id",
  "op": "inject|profile|query|reset",
  "target": {
    "bus": "vrtlbus0",
    "device": "uart0"
  },
  "ts": {
    "mode": "immediate|at_monotonic_ns|after_ns",
    "value": 0
  },
  "args": {}
}
```

- `id`: correlates request/response
- `target.device` may be omitted for bus-wide operations
- `ts` controls when the action becomes active (important for determinism)

### Responses

Server replies with one JSON line:

```json
{ "id": "…", "ok": true, "result": { } }
```

or

```json
{ "id": "…", "ok": false, "error": { "code": "EINVAL", "message": "…" } }
```

### Standard ops

#### `query`

- `args.kind`: `"buses"|"devices"|"stats"|"capabilities"`

#### `reset`

- `args.scope`: `"bus"|"device"|"all"`

#### `profile`

Apply a named profile (bundle of sysfs values + injection rules).

- `args.name`: profile name
- `args.parameters`: optional overrides

#### `inject`

Inject a fault/jitter rule.

- `args.rule` examples:

```json
{
  "kind": "delay",
  "direction": "rx|tx|both",
  "ns": 500000
}
```

```json
{
  "kind": "drop",
  "direction": "rx",
  "rate_ppm": 20000,
  "burst": { "on_frames": 10, "off_frames": 100 }
}
```

```json
{
  "kind": "priority_inversion",
  "scope": "kernel_worker",
  "duration_ns": 2000000
}
```

> The last example is "advanced": it assumes the core provides hooks to stress worker priorities/locks. If this is too invasive for v1, keep it as a future capability.

---

## 5) CLI conventions (`virtrtlabctl`)

Command structure:

- Discovery:
  - `virtrtlabctl list buses`
  - `virtrtlabctl list devices`
- Sysfs convenience:
  - `virtrtlabctl get uart0 baud`
  - `virtrtlabctl set uart0 baud=115200 parity=none`
- Profiles:
  - `virtrtlabctl profile apply uart_stress --device uart0`
- Injection:
  - `virtrtlabctl inject uart0 delay --rx 500us --after 10ms`
  - `virtrtlabctl inject can0 drop --rate 20000ppm --burst 10/100`
- CI helpers:
  - `virtrtlabctl run scenario deadlock_hunt --duration 60s --seed 1234`

Output rules:

- Human-readable by default
- `--json` for machine parsing
- Exit codes:
  - `0` success
  - `2` invalid args
  - `3` transport error
  - `4` kernel rejected op

---

## 6) Determinism and CI guidelines

To make CI results meaningful:

- Prefer time-based activation (`ts.mode`) over "immediate" where possible.
- Make stochastic behavior reproducible:
  - explicit `seed` (bus-level)
  - record seeds and profile names in artifacts
- Always export stats:
  - `virtrtlabctl query stats --json > artifacts/virtrtlab-stats.json`

Recommended CI pattern:

1. Boot test image / VM (or container with privileged kernel access)
2. Load modules (`virtrtlab_core` + needed peripherals)
3. Create `vrtlbus0` and devices via sysfs or module params
4. Run AUT test suite under different VirtRTLab profiles
5. Collect logs + VirtRTLab stats + kernel traces (ftrace, lockdep, perf sched)

---

## 7) v1 scope (suggested)

Start with a minimal, valuable slice:

- `virtrtlab_core` + one peripheral (`virtrtlab_uart` or `virtrtlab_can`)
- sysfs config for latency/jitter/drop/bitflip
- control socket with `inject/query/reset`
- per-device stats counters

Then iterate:

- record/replay (capture a real trace and replay it)
- burst models, correlated jitter
- integration with tracing (tracepoints for injections applied)

---

## 8) Open questions (to decide early)

- Kernel↔userspace control transport:
  - netlink vs misc char device vs configfs
- Device exposure strategy:
  - standalone sysfs-only vs also registering into existing subsystems (e.g. `tty`, `socketcan`, `spidev`)
- Safety:
  - permissions for `/run/virtrtlab.sock`
  - capability checks for injection operations

---

## License

TBD
