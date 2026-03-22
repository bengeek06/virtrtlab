<!-- SPDX-License-Identifier: CC-BY-4.0 -->

# `virtrtlabctl` — v0.2.0 Draft Additions

This document contains **draft CLI specification** for `v0.2.0` features that are not implemented in `v0.1.x` yet.

It exists to avoid mixing forward-looking interface design with the current stable CLI reference in [virtrtlabctl.md](virtrtlabctl.md).

The source of truth for the simulator runtime model remains [simulator-contract-v0.2.0.md](simulator-contract-v0.2.0.md).

---

## 1. `sim` command family

The planned `v0.2.0` simulator-management surface is:

```text
virtrtlabctl sim list [--type TYPE] [--verbose]
virtrtlabctl sim inspect <name>
virtrtlabctl sim attach <device> <simulator> [--auto-start] [--set key=value ...]
virtrtlabctl sim detach <device>
virtrtlabctl sim start <device>
virtrtlabctl sim stop <device>
virtrtlabctl sim status [<device>]
virtrtlabctl sim logs <device> [--stderr] [--tail N] [--follow]
```

All `sim` commands inherit the global `--json` flag documented in [virtrtlabctl.md](virtrtlabctl.md).

When `--json` is used:

- successful output must be valid JSON on stdout
- failures must use the stable error envelope `{"error": "...", "code": N}`
- human-oriented aligned text is suppressed from stdout

Human-readable mode contract:

- human-readable output is intended for operators, not for long-term script parsing
- successful command results go to stdout
- command errors and diagnostics go to stderr
- no ANSI color or terminal-control sequences are required in `v0.2.0`
- status words remain lowercase in field values: `attached`, `starting`, `running`, `stopping`, `stopped`, `failed`
- the only prefixed message markers standardized in `v0.2.0` are `[ok]`, `[info]`, `[warn]`, and `[error]`
- messages should use stable imperative or past-tense wording such as `attached`, `started`, `stopped`, `detached`, `failed to start`

Human-readable compatibility guidance:

- JSON is the scripting contract; human-readable output may evolve cosmetically
- nevertheless, command examples and golden tests may rely on stable field labels, marker words, and column order inside `v0.2.x`

### `sim list`

Lists catalog entries visible after precedence resolution.

```sh
virtrtlabctl sim list
virtrtlabctl sim list --type uart
virtrtlabctl sim list --verbose
```

Default output example:

```text
loopback             supports=uart         Echo bytes back to the same VirtRTLab link
test-stub            supports=uart         Deterministic simulator process for VirtRTLab CI and CLI validation
ublox-m8-nmea        supports=uart         Simulated u-blox GPS speaking NMEA
```

Column contract:

- output is one simulator per line
- columns appear in this order: `name`, `supports=...`, summary text
- alignment spaces are cosmetic and may vary
- the `supports=` key spelling is stable within `v0.2.x`

Verbose output additionally shows the source file that defined each entry and whether it overrides a lower-precedence catalog definition.

JSON output example:

```json
{
  "simulators": [
    {
      "name": "loopback",
      "supports": ["uart"],
      "summary": "Echo bytes back to the same VirtRTLab point-to-point link",
      "catalog_file": "/usr/share/virtrtlab/simulators.d/loopback.toml",
      "overrides": false
    }
  ]
}
```

### `sim inspect`

Displays full metadata for one simulator entry.

```sh
virtrtlabctl sim inspect loopback
virtrtlabctl sim inspect test-stub
```

Example output:

```text
name            loopback
supports        uart
summary         Echo bytes back to the same VirtRTLab point-to-point link
catalog file    /usr/share/virtrtlab/simulators.d/loopback.toml
restart policy  never

parameters:
  delay_ms      type=u32   required=no   default=0
```

Field-order contract:

- labeled lines appear in this order: `name`, `supports`, `summary`, `catalog file`, `restart policy`
- the `parameters:` heading is lowercase and ends with `:`
- each parameter line lists `name`, `type=...`, `required=...`, and `default=...` in that order

JSON output example:

```json
{
  "name": "loopback",
  "supports": ["uart"],
  "summary": "Echo bytes back to the same VirtRTLab point-to-point link",
  "description": "Reference simulator used for smoke tests and contract validation",
  "catalog_file": "/usr/share/virtrtlab/simulators.d/loopback.toml",
  "restart_policy": "never",
  "parameters": [
    {
      "name": "delay_ms",
      "type": "u32",
      "required": false,
      "default": 0,
      "description": "Delay before echoing received bytes back to the AUT"
    }
  ]
}
```

Illustrative `test-stub` output:

```text
name            test-stub
supports        uart
summary         Deterministic simulator process for VirtRTLab CI and CLI validation
catalog file    /usr/share/virtrtlab/simulators.d/test-stub.toml
restart policy  never

parameters:
  mode              type=string required=no default=run
  startup_delay_ms  type=u32    required=no default=0
  runtime_ms        type=u32    required=no default=0
  exit_code         type=u32    required=no default=1
```

### `sim attach`

Attaches a catalog simulator to one VirtRTLab device without starting it.

```sh
virtrtlabctl sim attach uart0 loopback
virtrtlabctl sim attach uart0 test-stub --set mode=crash --set runtime_ms=50
virtrtlabctl sim attach uart1 ublox-m8-nmea --auto-start --set scenario_file=./scenarios/static-fix.toml
virtrtlabctl sim attach uart1 gps --set profile='{"boot_ms":20,"scenario":{"mode":"urban"}}'
virtrtlabctl sim attach uart1 gps --set profile.scenario.mode=urban
```

Behaviour:

- validates that the target device exists
- validates that the simulator exists and supports the device type
- validates every `--set key=value` assignment against the simulator parameter declarations
- writes runtime attachment state under the VirtRTLab runtime directory
- does not start the simulator process automatically unless the command is later combined with `sim start` or a profile-driven `up`

`--set` contract in `v0.2.0`:

- syntax is `--set <path>=<value>`
- `<path>` is relative to `attachments.config`
- dotted paths such as `profile.scenario.mode` are allowed
- array indexing syntax is not supported in `v0.2.0`
- scalar values use plain CLI text
- structured values must use JSON literals for arrays and objects
- repeated `--set` on the same path is allowed and the last value wins

Validation rules:

- the first path segment must match a declared top-level parameter
- nested paths are allowed only for parameters declared as structured `object` or `array`
- invalid JSON literals, type mismatches, and integer overflow are errors

Human-readable success example:

```text
[ok] attached loopback to uart0
```

Success-message contract:

- successful attach uses the wording `[ok] attached <simulator> to <device>`
- replacement of an existing stopped or failed attachment may emit an additional `[info]` line before the final success line

JSON output example:

```json
{
  "device": "uart0",
  "simulator": "loopback",
  "state": "attached",
  "auto_start": false
}
```

### `sim detach`

Removes the attachment from a device.

```sh
virtrtlabctl sim detach uart0
```

If a simulator is currently running for the device, it is stopped first.

Cleanup semantics:

- successful detach removes the whole per-device runtime directory under `/run/virtrtlab/simulators/<device>/`
- detached state is not persisted across reboot or runtime-directory loss

Human-readable success example:

```text
[ok] detached simulator from uart0
```

Success-message contract:

- successful detach uses the wording `[ok] detached simulator from <device>`
- detach-triggered stop diagnostics, if any, precede the final detach line

JSON output example:

```json
{
  "device": "uart0",
  "state": "detached"
}
```

### `sim start`

Starts the simulator attached to one device.

```sh
virtrtlabctl sim start uart0
```

Behaviour:

- launches the simulator using its catalog `exec` and `args`
- passes runtime context through the environment variables defined in [simulator-contract-v0.2.0.md](simulator-contract-v0.2.0.md)
- transitions `attached -> starting -> running` once the process has been spawned successfully and has not exited immediately
- does not wait for a simulator-specific readiness probe in `v0.2.0`

Human-readable success example:

```text
[ok] started loopback on uart0
```

Success-message contract:

- successful start uses the wording `[ok] started <simulator> on <device>`
- if the simulator reaches `failed` during immediate launch checks, the human-readable error should start with `[error] failed to start <simulator> on <device>:`

JSON output example:

```json
{
  "device": "uart0",
  "simulator": "loopback",
  "state": "running",
  "pid": 14523
}
```

Concurrency note:

- concurrent `sim start` or `sim stop` requests on the same device are serialized
- once the command acquires the device lock, it reevaluates current state before acting

### `sim stop`

Stops the simulator attached to one device.

```sh
virtrtlabctl sim stop uart0
```

Behaviour:

- transitions `running -> stopping` by sending a graceful termination signal first
- force-kills the process if it does not exit within a bounded timeout
- keeps the attachment definition and leaves it in `stopped` state after the process exit is observed

Human-readable success example:

```text
[ok] stopped simulator on uart0
```

Success-message contract:

- successful stop uses the wording `[ok] stopped simulator on <device>`
- force-kill fallback may emit one preceding `[warn]` line before the final success line

JSON output example:

```json
{
  "device": "uart0",
  "state": "stopped",
  "last_exit_code": 0
}
```

If another lifecycle command is already mutating the same attachment, `sim stop` waits only for a bounded lock-acquisition interval before failing with an operational error.

### `sim status`

Displays attachment state for one device or all attached devices.

```sh
virtrtlabctl sim status
virtrtlabctl sim status uart0
```

Default output example:

```text
uart0   simulator=loopback       state=running  pid=14523  auto_start=yes
uart1   simulator=ublox-m8-nmea  state=attached pid=-      auto_start=no
```

Aggregate-column contract:

- output is one attachment per line
- columns appear in this order: device name, `simulator=...`, `state=...`, `pid=...`, `auto_start=...`
- `pid=-` is used when no live process exists
- `auto_start` values are rendered as `yes` or `no`
- alignment spaces are cosmetic and may vary

Detailed single-device output example:

```text
device          uart0
simulator       loopback
state           running
pid             14523
auto_start      yes
config file     /run/virtrtlab/simulators/uart0/config.toml
log dir         /run/virtrtlab/simulators/uart0/logs
```

Detailed-field contract:

- labeled lines appear in this order: `device`, `simulator`, `state`, `pid`, `auto_start`, `config file`, `log dir`
- when present for failure cases, `last error`, `last exit code`, and `stopped at` are appended after `log dir` in that order
- labels are lowercase and space-separated exactly as shown

Runtime state is stored under `/run/virtrtlab/simulators/`; see [simulator-contract-v0.2.0.md](simulator-contract-v0.2.0.md) for the exact layout.

The per-device state file is `/run/virtrtlab/simulators/<device>/state.json` and is the source of truth for lifecycle state, timestamps, PID, and the last observed failure.

If `/run/virtrtlab/simulators/` does not exist, `sim status` without a device argument returns an empty set.
If `/run/virtrtlab/simulators/<device>/state.json` does not exist, `sim status <device>` returns a not-found error.

When the attachment is in `failed`, the detailed human-readable output must also include `last error`, `last exit code`, and `stopped at`.

JSON output example for `sim status uart0`:

```json
{
  "schema_version": 1,
  "device": "uart0",
  "device_type": "uart",
  "simulator": "loopback",
  "instance_id": "uart0-20260322T184200Z-14523",
  "state": "running",
  "auto_start": true,
  "restart_policy": "never",
  "catalog_file": "/usr/share/virtrtlab/simulators.d/loopback.toml",
  "config_file": "/run/virtrtlab/simulators/uart0/config.toml",
  "log_dir": "/run/virtrtlab/simulators/uart0/logs",
  "pid": 14523,
  "last_exit_code": null,
  "last_error": null,
  "created_at": "2026-03-22T18:40:10Z",
  "started_at": "2026-03-22T18:42:00Z",
  "stopped_at": null,
  "updated_at": "2026-03-22T18:42:00Z"
}
```

JSON output example for `sim status` without a device argument:

```json
{
  "schema_version": 1,
  "attachments": [
    {
      "device": "uart0",
      "state": "running",
      "simulator": "loopback",
      "pid": 14523,
      "auto_start": true,
      "updated_at": "2026-03-22T18:42:00Z"
    }
  ]
}
```

Consistency rules:

- `sim status <device>` must return one coherent per-device snapshot
- `sim status` without a device may observe different devices at slightly different moments, but each attachment row or object must be internally coherent

### `sim logs`

Prints managed logs for the simulator attached to one device.

```sh
virtrtlabctl sim logs uart0
virtrtlabctl sim logs uart0 --stderr
virtrtlabctl sim logs uart0 --tail 50
virtrtlabctl sim logs uart0 --follow
```

Default behaviour reads the attachment stdout log. `--stderr` selects the stderr log instead.

`--follow` behaves like `tail -f`: it keeps the command attached to the selected log file and streams appended lines until interrupted.

`--tail N --follow` is allowed and starts by printing the last `N` lines before switching to follow mode.

`sim logs` is intentionally stream-oriented. In `v0.2.0`, `--json` is not required for `sim logs`; JSON mode is reserved for non-streaming `sim` subcommands.

Human-readable stream contract:

- log payload lines are emitted verbatim, without `[ok]` or `[info]` prefixes
- command-level errors, such as missing attachment state or missing log file, are emitted as normal CLI errors rather than mixed into the log stream
- `--tail N` preserves original log line ordering

Because logs live under `/run`, they are ephemeral.
After `sim detach`, `down`, reboot, or manual runtime cleanup, `sim logs <device>` may legitimately return a not-found error.

`sim logs` does not acquire the lifecycle mutation lock for the whole duration of follow mode.
It reads the current log file view and tolerates log growth or process exit while following.

### Built-in `loopback`

`loopback` is the mandatory built-in simulator for `v0.2.0`.

It exists to:

- validate the end-to-end UART path quickly
- exercise the catalog and lifecycle model without protocol complexity
- serve as the reference simulator for third-party authors

In `v0.2.0`, `loopback` is specified as a UART-only simulator with one optional parameter:

```toml
[[parameters]]
name = "delay_ms"
type = "u32"
required = false
default = 0
description = "Delay before echoing received bytes back to the AUT"
```

`restart_policy` visibility note:

- `sim inspect` must expose the declared restart policy
- `sim status <device>` must expose the effective restart policy from runtime state
- in `v0.2.0`, `on-failure` is informational only and does not cause automatic restart by itself

`test-stub` note:

- `test-stub` is the preferred target for CLI golden tests, crash tests, and log-capture tests
- `loopback` remains the preferred target for end-to-end socket smoke tests

### Exit codes for `sim` commands

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Operational error (spawn failure, stop timeout, filesystem error) |
| 2 | Invalid arguments or invalid `--set key=value` syntax |
| 3 | State conflict (already running, already attached, already starting) |
| 4 | Not found or incompatible target (unknown simulator, unknown device, unsupported device type, no attachment) |

Recommended error mapping:

| Condition | Exit code |
|---|---|
| catalog file parse error | 1 |
| runtime state I/O error | 1 |
| simulator spawn failure | 1 |
| stop timeout after forced termination failure | 1 |
| malformed `--set key=value` | 2 |
| invalid value type for declared parameter | 2 |
| missing required positional argument | 2 |
| `sim start` while already `running` | 3 |
| `sim start` while already `starting` | 3 |
| lifecycle lock acquisition timeout | 1 |
| unknown device | 4 |
| unknown simulator | 4 |
| unsupported device type for selected simulator | 4 |
| `sim start` on detached device | 4 |

JSON failure examples:

```json
{"error": "unknown simulator: ublox-m9", "code": 4}
```

```json
{"error": "state conflict: uart0 is already running", "code": 3}
```

### State transitions

The CLI contract exposes the following nominal lifecycle:

```text
detached -> attached -> starting -> running -> stopping -> stopped
                              \-> failed
running ----------------------> failed
failed -> starting -> running
stopped -> starting -> running
attached|stopped|failed -> detached
```

`failed` represents either:

- a launch-time error during `sim start`
- an unexpected process exit after the simulator was already `running`

The precise `state.json` schema and transition rules are defined in [simulator-contract-v0.2.0.md](simulator-contract-v0.2.0.md).

---

## 2. Draft profile additions

The current `v0.1.x` lab profile supports only device and bus configuration.

The following `v0.2.0` extension is planned for simulator attachments:

```toml
[[devices]]
type = "uart"
count = 2

[[attachments]]
device = "uart0"
simulator = "loopback"
auto_start = true

[attachments.config]
delay_ms = 0
```

`virtrtlabctl up --config ...` will validate `[[attachments]]` before partial startup.
Only attachments with `auto_start = true` are started automatically.

### `up --config` with simulator attachments

When a profile contains `[[attachments]]`, `up --config` behaves as follows in `v0.2.0`:

1. validate the whole profile, including attachment declarations and parameter shapes
2. materialize runtime attachment state for declared attachments
3. auto-start only the attachments with `auto_start = true`

Failure semantics:

- validation errors abort before any simulator process is started
- if one auto-started attachment fails to launch, `up --config` exits non-zero
- already-started attachments are left running
- the failed attachment is left in `failed` state
- attachments not yet started remain `attached`

Human-readable output must make partial startup explicit.

Illustrative example:

```text
[ok] attached loopback to uart0
[ok] started loopback on uart0
[ok] attached gps to uart1
[error] failed to start gps on uart1: socket connect failed
[info] simulator startup is partial; inspect with `virtrtlabctl sim status`
```

Partial-startup wording contract:

- successful lifecycle events during `up --config` reuse the same `[ok] attached ...` and `[ok] started ...` wording as direct `sim` subcommands
- attachment launch failure uses `[error] failed to start <simulator> on <device>: <reason>`
- partial overall outcome is summarized by one trailing `[info]` line

Illustrative JSON failure shape:

```json
{
  "error": "simulator auto-start failed for uart1",
  "code": 1,
  "attachments": [
    {
      "device": "uart0",
      "state": "running"
    },
    {
      "device": "uart1",
      "state": "failed"
    },
    {
      "device": "uart2",
      "state": "attached"
    }
  ]
}
```

This output is intended to make partial startup scriptable without requiring an immediate follow-up `sim status` call.