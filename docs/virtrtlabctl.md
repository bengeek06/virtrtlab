<!-- SPDX-License-Identifier: CC-BY-4.0 -->

# VirtRTLab — `virtrtlabctl` CLI (v1)

Source of truth is the root [README.md](../README.md). This file keeps CLI-specific
contracts focused.

## Overview

`virtrtlabctl` is the control CLI for VirtRTLab. It covers three concerns:

1. **Lab orchestration** — load kernel modules + start `virtrtlabd` from a lab profile
2. **Sysfs convenience** — read/write individual device attributes without raw `echo`/`cat`
3. **Daemon lifecycle** — start, stop, and query `virtrtlabd` independently of module loading

`virtrtlabctl` is implemented in **Python 3.11+** (stdlib only). It operates by reading
and writing sysfs files (`/sys/kernel/virtrtlab/`) and forking `virtrtlabd` as a child
process. It does **not** open the daemon sockets (`uart<N>.sock`) — those are reserved
for simulators.

---

## Command structure

```
virtrtlabctl [--json] [--no-sudo] <command> [<subcommand>] [<args>]
```

| Global flag | Description |
|-------------|-------------|
| `--json` | Switch all output to machine-readable JSON (see [Output format](#output-format)) |
| `--no-sudo` | Do not prepend `sudo` to privileged operations (`insmod`, `rmmod`, `virtrtlabd`). Use when the caller already has the required capabilities (e.g. root shell, CI container) |

### `up` — bring up a lab profile

```
virtrtlabctl up [--config <file>] [--uart <N>] [--gpio <N>]
```

Loads the required kernel modules and starts `virtrtlabd` according to a lab profile.
Idempotent if the profile is already active (modules already loaded, daemon already
running); prints a warning and exits `0`.

Steps performed in order:

1. Resolve the lab profile (see [Lab profile](#lab-profile)).
2. For each device type in the profile (in declaration order):
   - Run `insmod <module>.ko <param>=<count>` as root if the module is not already loaded.
   - On failure: unload already-loaded modules in reverse order, exit `1`.
3. Start `virtrtlabd --num-uarts <N> --run-dir /run/virtrtlab` as root in the background.
   Write the daemon PID to `/run/virtrtlab/daemon.pid`.
   Write the ordered list of loaded modules to `/run/virtrtlab/modules.list`.
4. Poll for the appearance of **all** expected sockets (`uart0.sock` … `uart<N-1>.sock`)
   for up to 5 seconds. Exit `3` on timeout. Waiting for all sockets is required: a
   test connecting to `uart1.sock` immediately after `up` returns must not race against
   a partially-started daemon.

Options:

| Option | Description |
|--------|-------------|
| `--config <file>` | Path to a TOML lab profile (default: `./lab.toml`, then `/etc/virtrtlab/lab.toml`) |
| `--uart <N>` | Inline override: N UART instances. Generates a transient profile; no file needed. |
| `--gpio <N>` | Inline override: N GPIO instances. |

Inline overrides (`--uart`, `--gpio`, …) take precedence over `--config`.
If both a config file and inline overrides are present, the inline values replace
the corresponding sections of the file profile; unmentioned device types come from the file.

### `down` — tear down a lab

```
virtrtlabctl down
```

Stops `virtrtlabd` (SIGTERM + wait up to 5 s, then SIGKILL), then runs `rmmod` in
reverse module insertion order. Module list is read from `/run/virtrtlab/modules.list`
(written by `up`).

If `modules.list` is absent, `down` prints a warning to stderr and attempts `rmmod`
on all known modules (`virtrtlab_gpio`, `virtrtlab_uart`, `virtrtlab_core`) in that
order (reverse of the standard load order: core last). Exit `0` even if some modules were already unloaded; a missing module is not
an error on teardown.

### `status` — global lab status

```
virtrtlabctl status
```

Reports:

- Whether each known module is loaded (`/proc/modules`)
- Daemon PID and running state (`/run/virtrtlab/daemon.pid` + `/proc/<pid>/status`)
- List of active sockets under `/run/virtrtlab/`
- Bus state (`/sys/kernel/virtrtlab/buses/vrtlbus0/state`)

Human output example:

```
modules:
  virtrtlab_core   loaded
  virtrtlab_uart   loaded (2 instances)
  virtrtlab_gpio   not loaded

daemon:
  pid    12345
  state  running

sockets:
  /run/virtrtlab/uart0.sock
  /run/virtrtlab/uart1.sock

bus vrtlbus0:
  state  up
```

JSON output: see [Output format](#output-format).

### `list` — discover buses and devices

```
virtrtlabctl list buses
virtrtlabctl list devices [--type <type>]
```

Reads `/sys/kernel/virtrtlab/buses/` and `/sys/kernel/virtrtlab/devices/` respectively.
`--type` filters by the `type` sysfs attribute (`uart`, `gpio`, etc.).

### `get` — read a sysfs attribute

```
virtrtlabctl get <device> <attr>
virtrtlabctl get bus <attr>
```

Reads `/sys/kernel/virtrtlab/devices/<device>/<attr>` or
`/sys/kernel/virtrtlab/buses/vrtlbus0/<attr>` and prints the value (trailing newline
stripped).

Examples:

```bash
virtrtlabctl get uart0 baud          # → 115200
virtrtlabctl get uart0 latency_ns    # → 0
virtrtlabctl get bus state           # → up
```

If the attribute file does not exist: print error to stderr, exit `4`.
If `open()` or `read()` returns an error: print the errno message, exit `4`.

### `set` — write a sysfs attribute

```
virtrtlabctl set <device> <attr>=<value> [<attr>=<value> …]
virtrtlabctl set bus <attr>=<value>
```

Writes each `<attr>=<value>` pair to the corresponding sysfs file.
Multiple pairs are applied left-to-right; a failure on any pair stops the sequence
and exits `4` (previously applied pairs are **not** rolled back — sysfs writes are
not transactional).

Examples:

```bash
virtrtlabctl set uart0 latency_ns=500000 drop_rate_ppm=20000
virtrtlabctl set bus state=down
```

| Exit code | Condition |
|-----------|-----------|
| `0` | All writes accepted by the kernel |
| `2` | `<attr>=<value>` parse error |
| `4` | Kernel returned an error (`-EINVAL`, `-EBUSY`, `-EIO`, …) |

### `stats` — display per-device counters

```
virtrtlabctl stats <device>
```

Reads all files under `/sys/kernel/virtrtlab/devices/<device>/stats/` and prints them.

Human output example:

```
uart0 stats:
  tx_bytes     1048576
  rx_bytes     4096
  drops        3
  overruns     0
  bitflips     1
```

### `reset` — reset stats counters

```
virtrtlabctl reset <device>
```

Writes `0` to `/sys/kernel/virtrtlab/devices/<device>/stats/reset`.

This resets stats counters only. For a full device reset (fault attrs + `enabled` + stats),
write `reset` to the bus state attribute:

```bash
virtrtlabctl set bus state=reset
```

### `daemon` — manage `virtrtlabd` independently

```
virtrtlabctl daemon start [--num-uarts <N>] [--run-dir <dir>]
virtrtlabctl daemon stop
virtrtlabctl daemon status
```

`daemon start` launches `virtrtlabd` as root (via `sudo` unless `--no-sudo`) in the
background and writes the PID to `/run/virtrtlab/daemon.pid`. Fails with exit `3` if
a daemon is already running (pid-file exists and process is alive).

`daemon stop` sends SIGTERM to the recorded PID, waits up to 5 s for the process to
exit, then sends SIGKILL if still running.

`daemon status` prints daemon PID and running state. Exit `3` if not running.

---

## Lab profile

A lab profile is a TOML file describing the desired hardware configuration.

```toml
# lab.toml

[build]
module_dir = "/home/user/projects/virtrtlab/kernel"  # optional; path to .ko files

[bus]
seed = 42          # optional; written to /sys/.../buses/vrtlbus0/seed after up

[[devices]]
type  = "uart"
count = 2

[[devices]]
type  = "gpio"
count = 1
```

The `[build]` section is optional. If `module_dir` is set, `.ko` files are searched
there first, then `./`, then `/lib/modules/$(uname -r)/`. This covers the common
development case where modules are built in-tree without `make install`.

### Profile resolution order

1. `--config <file>` if specified
2. `./lab.toml` in the current working directory
3. `/etc/virtrtlab/lab.toml`
4. If none found and no inline overrides: error, exit `2`

### Module mapping

| `type` in profile | Kernel module | `insmod` parameter |
|---|---|---|
| `uart` | `virtrtlab_uart.ko` | `num_uarts=<count>` |
| `gpio` | `virtrtlab_gpio.ko` | `num_gpio=<count>` |

Any unknown `type` value causes `up` to fail immediately with exit `2` and a clear
error message (`unknown device type: <type>`). SPI, ADC, DAC types are reserved for
v0.2.0 and are not supported in v0.1.0.

Module search order: `[build].module_dir` (if set) → `./` → `/lib/modules/$(uname -r)/`.

---

## Exit codes

| Code | Meaning |
|------|---------|
| `0` | Success |
| `1` | Module load/unload failure |
| `2` | Invalid arguments or profile parse error |
| `3` | Daemon error (not running, timeout, socket missing) |
| `4` | Kernel attribute write rejected or sysfs path missing |

---

## Output format

### Human-readable (default)

Key-value pairs or plain values, one per line. No fixed schema — optimised for
readability in a terminal.

### JSON (`--json`)

A single JSON object is printed to stdout on success. On error, a JSON object with
an `"error"` key is printed to stdout and the process exits with the appropriate code.

`list devices --json` example:

```json
{
  "devices": [
    {"name": "uart0", "type": "uart", "bus": "vrtlbus0", "enabled": true},
    {"name": "uart1", "type": "uart", "bus": "vrtlbus0", "enabled": true}
  ]
}
```

`stats uart0 --json` example:

```json
{
  "device": "uart0",
  "stats": {
    "tx_bytes": 1048576,
    "rx_bytes": 4096,
    "drops": 3,
    "overruns": 0,
    "bitflips": 1
  }
}
```

Error example:

```json
{"error": "kernel rejected write to latency_ns: Invalid argument", "code": 4}
```

---

## Rationale

**Python over C** — `virtrtlabctl` is a control tool, not a relay. Its operations
are sysfs file reads/writes and process lifecycle management. There is no hot path, no
latency constraint, and no GC interference risk. Python argparse, `pathlib`, `tomllib`
(stdlib since 3.11), and `subprocess` cover all requirements without additional
dependencies. C would add ~300 lines of boilerplate for argument parsing alone with
no observable benefit.

**TOML for lab profiles** — TOML is human-readable, supports typed arrays of tables
(`[[devices]]`) cleanly, and is in the Python stdlib since 3.11 (`tomllib`). YAML
requires `pyyaml` (external dependency). INI (`configparser`) cannot represent arrays
of typed sections without custom parsing. JSON lacks comments, which are useful for
documenting seed values and device counts in committed CI profiles.

**`up`/`down` as first-class commands** — device count is a property of the lab, not
of individual CLI invocations. Expressing it in a committed `lab.toml` makes the
configuration reviewable and reproducible in CI. Inline `--uart`/`--gpio` flags provide
a one-liner escape for scripting without a file.

**`daemon start`/`stop` separate from `up`/`down`** — `up` and `down` are full lab
lifecycle operations (modules + daemon). `daemon start`/`stop` allows controlling the
daemon independently when the kernel modules are managed externally (e.g. by a test
fixture that loads/unloads modules per test class).

**Stats reset writes `0`, not `reset`** — writing `reset` to `stats/reset` is kernel
write-only by spec (returns `-EPERM` on read). The value `0` is the documented trigger
(see `docs/sysfs.md`). Full device reset (including fault attrs) requires writing
`reset` to the bus `state` attribute, which is intentionally a separate, more
destructive operation.

**`sudo` automatic by default** — `virtrtlabctl` prepends `sudo` to all privileged
operations (`insmod`, `rmmod`, `virtrtlabd start`). This matches the UX of common
system tools. `--no-sudo` disables it for callers that already hold the required
capabilities (root shell, CI container with `CAP_SYS_MODULE`).

**`module_dir` in `[build]`** — out-of-tree development is the primary use case during
v0.1.0; requiring `make install` before `virtrtlabctl up` would block fast iteration.
The `[build].module_dir` key covers this without polluting device sections.

**`up` waits for all sockets** — the instance count is known at poll time (from the
resolved profile). Waiting only for `uart0.sock` would be a false signal of readiness
for any test that connects to `uart1.sock` or higher immediately after `up`.

**`down` without `modules.list` warns, does not fail** — teardown must never leave
the system in a worse state. A best-effort rmmod on known modules with a warning is
safe; a hard failure would require manual cleanup.

**SPI/ADC/DAC deferred to v0.2.0** — unknown type values fail fast with a clear error
in v0.1.0 rather than silently succeeding or ignoring the section.
