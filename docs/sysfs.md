# VirtRTLab sysfs (v1)

Source of truth is the root [README.md](../README.md). This file exists to keep sysfs-specific discussions focused.

## Base path

Recommended: `/sys/kernel/virtrtlab/`

## Root

- `version` (ro)
- `build_id` (ro)
- `buses/`
- `devices/`
- `profiles/` (optional)
- `stats/`

## Buses

`/sys/kernel/virtrtlab/buses/vrtlbus0/`

- `state` (rw): `up|down|reset`
- `clock_ns` (ro)
- `seed` (rw)
- `default_policy` (rw)

## Devices (common)

`/sys/kernel/virtrtlab/devices/<dev>/`

- `type` (ro)
- `bus` (ro)
- `enabled` (rw)
- `mode` (rw): `normal|record|replay`
- `latency_ns` (rw)
- `jitter_ns` (rw)
- `drop_rate_ppm` (rw)
- `bitflip_rate_ppm` (rw)
- `fault_policy` (rw)
- `stats/` (ro)
