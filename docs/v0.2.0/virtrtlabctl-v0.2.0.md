<!-- SPDX-License-Identifier: CC-BY-4.0 -->

# `virtrtlabctl` — v0.2.0 Draft Additions

This document contains **draft CLI specification** for `v0.2.0` features that are not implemented in `v0.1.x` yet.

It exists to avoid mixing forward-looking interface design with the current stable CLI reference in [../virtrtlabctl.md](../virtrtlabctl.md).

In `v0.2.0`, `virtrtlabctl` is a client of the daemon control plane. The
canonical write-side control path is the daemon control socket specified in
[control-socket-v0.2.0.md](control-socket-v0.2.0.md).

The source of truth for the simulator runtime model remains
[simulator-contract-v0.2.0.md](simulator-contract-v0.2.0.md). The broader
`v0.2.0` architecture is defined in [spec-v0.2.0.md](spec-v0.2.0.md).

## 0. Control-plane assumptions

Unless stated otherwise, every `v0.2.0` CLI command described in this document:

- connects to `/run/virtrtlab/control.sock`
- sends one structured control request to the daemon
- renders the daemon result in human-readable or JSON CLI form

The CLI is therefore not the source of truth for topology or simulator state.
It is the stable operator-facing frontend for the daemon-owned control plane.

---

## 1. `sim` command family

The planned `v0.2.0` simulator-management surface is:

```text
virtrtlabctl sim list [--type TYPE] [--verbose]
virtrtlabctl sim inspect <name> [--version VERSION]
virtrtlabctl sim attach <device> <simulator> [--version VERSION] [--auto-start] [--set key=value ...]
virtrtlabctl sim detach <device>
virtrtlabctl sim start <device>
virtrtlabctl sim stop <device>
virtrtlabctl sim status [<device>]
virtrtlabctl sim logs <device> [--stderr] [--tail N] [--follow]
```

All `sim` commands accept the global `--json` flag documented in [../virtrtlabctl.md](../virtrtlabctl.md).

When `--json` is used:

- for non-streaming commands (`sim list`, `sim inspect`, `sim attach`, `sim detach`, `sim start`, `sim stop`, `sim status`), successful output must be a single valid JSON document on stdout
- for streaming commands (`sim logs`), JSON mode is not supported: the command MUST fail fast, print the standard error envelope `{"error": "...", "code": N}` on stdout, write a brief diagnostic to stderr, and exit with a stable non-zero status code
- failures for all commands must use the stable error envelope `{"error": "...", "code": N}`
- human-oriented aligned text is suppressed from stdout

For successful non-streaming commands, the JSON document printed by the CLI is
exactly the daemon `result` payload from the control socket, with the transport
envelope fields `id` and `ok` removed. The CLI must not rename fields, change
types, or restructure the payload.

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

Golden-fixture guidance:

- golden fixtures should exist for both human-readable and JSON forms of `sim list`, `sim inspect`, and `sim status`
- fixture comparisons should treat alignment whitespace as cosmetic in human-readable mode
- fixture comparisons must remain strict on field names, field order, marker words, and JSON key presence

### `sim list`

Lists catalog entries visible after precedence resolution.

```sh
virtrtlabctl sim list
virtrtlabctl sim list --type uart
virtrtlabctl sim list --verbose
```

Default output example:

```text
loopback      version=1.0.0  supports=uart  Echo bytes back to the same VirtRTLab link
test-stub     version=1.0.0  supports=uart  Deterministic simulator process for VirtRTLab CI and CLI validation
ublox-m8-nmea version=2.4.1  supports=uart  Simulated u-blox GPS speaking NMEA
```

Column contract:

- output is one simulator per line
- columns appear in this order: `name`, `version=...`, `supports=...`, summary text
- alignment spaces are cosmetic and may vary
- the `version=` key spelling is stable within `v0.2.x`
- the `supports=` key spelling is stable within `v0.2.x`

Verbose output additionally shows the source file that defined each entry and whether it overrides a lower-precedence catalog definition.

In `v0.2.0`, verbose list output should also expose the simulator version.

JSON output example:

```json
{
  "simulators": [
    {
      "name": "loopback",
      "version": "1.0.0",
      "supports": ["uart"],
      "summary": "Echo bytes back to the same VirtRTLab point-to-point link",
      "catalog_file": "/usr/share/virtrtlab/simulators.d/loopback.toml",
      "overrides": false
    }
  ]
}
```

Golden-fixture expectation:

- the human-readable golden file for `sim list` should compare normalized lines, not raw spacing width
- the JSON golden file for `sim list` should compare parsed JSON objects after stable key ordering

### `sim inspect`

Displays full metadata for one simulator entry.

```sh
virtrtlabctl sim inspect loopback
virtrtlabctl sim inspect test-stub
virtrtlabctl sim inspect loopback --version 1.0.0
```

Example output:

```text
name            loopback
version         1.0.0
supports        uart
summary         Echo bytes back to the same VirtRTLab point-to-point link
catalog file    /usr/share/virtrtlab/simulators.d/loopback.toml
restart policy  never

parameters:
  delay_ms      type=u32   required=no   default=0
```

Field-order contract:

- labeled lines appear in this order: `name`, `version`, `supports`, `summary`, `catalog file`, `restart policy`
- the `parameters:` heading is lowercase and ends with `:`
- each parameter line lists `name`, `type=...`, `required=...`, and `default=...` in that order

JSON output example:

```json
{
  "name": "loopback",
  "version": "1.0.0",
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
version         1.0.0
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

Golden-fixture expectation:

- `sim inspect` human-readable fixtures must preserve label order and parameter row order
- `sim inspect` JSON fixtures must preserve parameter array order as emitted by the command

When multiple visible versions share the same simulator name, `sim inspect <name>` without `--version` must fail with the daemon error `ambiguous-simulator-version`, which the CLI maps to exit code `2`.

### `sim attach`

Attaches a catalog simulator to one VirtRTLab device without starting it.

```sh
virtrtlabctl sim attach uart0 loopback
virtrtlabctl sim attach uart0 loopback --version 1.0.0
virtrtlabctl sim attach uart0 test-stub --set mode=crash --set runtime_ms=50
virtrtlabctl sim attach uart1 ublox-m8-nmea --auto-start --set scenario_file=./scenarios/static-fix.toml
virtrtlabctl sim attach uart1 gps --set profile='{"boot_ms":20,"scenario":{"mode":"urban"}}'
virtrtlabctl sim attach uart1 gps --set profile.scenario.mode=urban
```

Behaviour:

- validates that the target device exists
- validates that the simulator exists and supports the device type
- validates every `--set key=value` assignment against the simulator parameter declarations
- submits the attachment request to the daemon, which owns the runtime attachment state
- does not start the simulator process automatically unless the command is later combined with `sim start` or a profile-driven `up`

`--set` contract in `v0.2.0`:

- syntax is `--set <path>=<value>`
- `<path>` is relative to `attachments.config`
- dotted paths such as `profile.scenario.mode` are allowed
- array indexing syntax is not supported in `v0.2.0`
- scalar values use plain CLI text
- structured values must use JSON literals for arrays and objects
- repeated `--set` on the same path is allowed and the last value wins

Version-selection contract:

- `--version VERSION` pins attachment selection to one explicit simulator version
- if `--version` is omitted and multiple visible versions share the same simulator name, `sim attach` fails with the daemon error `ambiguous-simulator-version`, which the CLI maps to exit code `2`
- direct `sim attach` defaults to `auto_start = false` unless `--auto-start` is specified explicitly

Attachment replacement rules:

- `sim attach` replaces an existing attachment only when the current state is `attached`, `stopped`, or `failed`
- attempting replacement while the current attachment is `starting`, `running`, or `stopping` fails with a state conflict

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
  "version": "1.0.0",
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
  "version": "1.0.0",
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
uart0   simulator=loopback version=1.0.0       state=running  pid=14523  auto_start=yes
uart1   simulator=ublox-m8-nmea version=2.4.1  state=attached pid=-      auto_start=no
```

Aggregate-column contract:

- output is one attachment per line
- columns appear in this order: device name, `simulator=...`, `version=...`, `state=...`, `pid=...`, `auto_start=...`
- `pid=-` is used when no live process exists
- `auto_start` values are rendered as `yes` or `no`
- alignment spaces are cosmetic and may vary

Detailed single-device output example:

```text
device          uart0
simulator       loopback
simulator version 1.0.0
state           running
pid             14523
auto_start      yes
config file     /run/virtrtlab/simulators/uart0/config.toml
log dir         /run/virtrtlab/simulators/uart0/logs
```

Detailed-field contract:

- labeled lines appear in this order: `device`, `simulator`, `simulator version`, `state`, `pid`, `auto_start`, `config file`, `log dir`
- when present for failure cases, `last error`, `last exit code`, and `stopped at` are appended after `log dir` in that order
- labels are lowercase and space-separated exactly as shown

The daemon control plane is the source of truth for simulator lifecycle state.
The runtime files under `/run/virtrtlab/simulators/` are daemon-owned runtime
artifacts used to persist attachment state and logs; see
[simulator-contract-v0.2.0.md](simulator-contract-v0.2.0.md) for the exact
layout.

If the daemon is unavailable, `sim status` fails with a normal daemon-unavailable
error. If the daemon is available but no attachments exist, `sim status` without
a device argument returns an empty set. If the target device exists but has no
attachment, `sim status <device>` returns the daemon error `not-attached`, which
the CLI maps to exit code `4`.

When the attachment is in `failed`, the detailed human-readable output must also include `last error`, `last exit code`, and `stopped at`.

JSON output example for `sim status uart0`:

```json
{
  "schema_version": 1,
  "device": "uart0",
  "device_type": "uart",
  "simulator": "loopback",
  "simulator_version": "1.0.0",
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
      "simulator_version": "1.0.0",
      "pid": 14523,
      "auto_start": true,
      "updated_at": "2026-03-22T18:42:00Z"
    }
  ]
}
```

Golden-fixture expectation:

- aggregate `sim status` human-readable fixtures must preserve row order chosen by the command; the recommended order is lexical by device name
- detailed `sim status <device>` human-readable fixtures must preserve field order exactly
- per-device JSON fixtures must normalize only dynamic runtime values such as timestamps, PIDs, and instance IDs before comparison

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
- `sim inspect` and `sim status <device>` must expose the simulator version for validation traceability
- in `v0.2.0`, `on-failure` is informational only and does not cause automatic restart by itself

`test-stub` note:

- `test-stub` is the preferred target for CLI golden tests, crash tests, and log-capture tests
- `loopback` remains the preferred target for end-to-end socket smoke tests

### Exit codes for `sim` commands

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Operational error (spawn failure, stop timeout, filesystem error) |
| 2 | Invalid arguments, ambiguous simulator version selection, or invalid `--set key=value` syntax |
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
| ambiguous simulator version selection | 2 |
| `sim start` while already `running` | 3 |
| `sim start` while already `starting` | 3 |
| `sim attach` while an attachment is `running`, `starting`, or `stopping` | 3 |
| `sim stop` on an `attached` or `failed` attachment | 3 |
| lifecycle lock acquisition timeout | 1 |
| unknown device | 4 |
| unknown simulator | 4 |
| unsupported device type for selected simulator | 4 |
| `sim start` on detached device | 4 |
| `sim detach` on detached device | 4 |
| `sim status <device>` on unattached device | 4 |

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

`virtrtlabctl up --config ...` sends the resolved profile to the daemon, which
validates `[[attachments]]` before partial startup.
If `auto_start` is omitted, the effective default comes from the simulator catalog `default_auto_start`; otherwise it falls back to `false`.
Only attachments whose effective auto-start value is `true` are started automatically.

### `up --config` with simulator attachments

When a profile contains `[[attachments]]`, `up --config` behaves as follows in `v0.2.0`:

1. validate the whole profile, including attachment declarations and parameter shapes
2. materialize runtime attachment state for declared attachments
3. auto-start only the attachments whose effective auto-start value is `true`

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

### Golden fixtures

Recommended fixture layout under `tests/fixtures/`:

```text
tests/fixtures/
└── cli/
  └── sim/
    ├── list.default.txt
    ├── list.default.json
    ├── list.verbose.txt
    ├── list.verbose.json
    ├── inspect.loopback.txt
    ├── inspect.loopback.json
    ├── inspect.test-stub.txt
    ├── inspect.test-stub.json
    ├── status.aggregate.attached.txt
    ├── status.aggregate.attached.json
    ├── status.aggregate.running.txt
    ├── status.aggregate.running.json
    ├── status.device.running.txt
    ├── status.device.running.json
    ├── status.device.failed.txt
    └── status.device.failed.json
```

Naming contract:

- `<command>.<variant>.<ext>` for `sim list`
- `inspect.<simulator>.<ext>` for `sim inspect`
- `status.aggregate.<variant>.<ext>` and `status.device.<variant>.<ext>` for `sim status`
- `.txt` stores human-readable stdout
- `.json` stores canonical JSON output

Normalization rules for fixture comparison:

- human-readable fixtures normalize trailing spaces and final trailing newline only
- alignment runs of spaces may be collapsed for commands documented as column-aligned output: `sim list` and aggregate `sim status`
- labeled-field outputs such as `sim inspect` and detailed `sim status <device>` must preserve labels, order, and values, but may ignore padding width between label and value
- JSON fixtures must be compared structurally after JSON parsing, not as raw text
- dynamic values in JSON such as `pid`, `instance_id`, `created_at`, `started_at`, `stopped_at`, and `updated_at` should be replaced in the test harness with deterministic placeholders before fixture comparison

Recommended placeholders for normalized JSON comparisons:

| Dynamic field | Placeholder |
|---|---|
| `pid` | `12345` |
| `instance_id` | `INSTANCE_ID` |
| `created_at` | `TIMESTAMP` |
| `started_at` | `TIMESTAMP` |
| `stopped_at` | `TIMESTAMP` |
| `updated_at` | `TIMESTAMP` |

Recommended placeholders for normalized human-readable comparisons:

| Dynamic fragment | Placeholder |
|---|---|
| `version=1.0.0` | do not normalize |
| `pid=14523` | `pid=PID` |
| standalone PID field value | `PID` |
| timestamp values | `TIMESTAMP` |

Non-normalized fields:

- simulator names
- device names
- field labels
- marker words such as `[ok]` and `[error]`
- enum values such as `running` or `failed`
- rendered `auto_start=yes|no`

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