<!-- SPDX-License-Identifier: CC-BY-4.0 -->

# VirtRTLab Simulator Contract (v0.2 Draft)

This document is the source of truth for the simulator catalog and lifecycle contract targeted at `v0.2.0`.

It defines how `virtrtlabctl` discovers simulators, how a simulator is attached to a VirtRTLab device, and which runtime context is passed to the simulator process.

This contract belongs to VirtRTLab itself. Protocol-specific simulators such as GPS, sensor, switch-console, or register-oriented component peers may live in separate repositories, but they must conform to the rules defined here if they want to be managed by `virtrtlabctl`.

---

## 1. Scope

The simulator contract exists to make VirtRTLab usable as a complete test bench rather than a raw virtual link.

The contract covers:

- simulator catalog discovery
- user-extensible simulator definitions
- device-to-simulator attachment
- runtime context passed by `virtrtlabctl`
- lifecycle expectations for start, stop, failure, and teardown

The contract does not cover:

- the protocol implemented by a simulator
- a plugin ABI loaded in-process by Python or C
- a simulator marketplace or remote registry
- bundling complex protocol simulators inside the VirtRTLab repository

---

## 2. Design goals

The contract must satisfy the following goals:

- built-in and user-provided simulators follow the same model
- the first useful built-in simulator is `loopback`
- simulators remain ordinary executable processes, not custom kernel extensions
- profile-driven lab startup remains reproducible and version-controllable
- the runtime contract is explicit enough that third-party simulators can be written without reading VirtRTLab source code

---

## 3. Catalog format

### 3.1 Chosen format

The simulator catalog format is **TOML**.

Rationale:

- VirtRTLab already uses TOML for lab profiles
- TOML is readable enough for hand-written simulator entries
- the Python CLI can parse it with standard library support on supported runtimes
- string arrays and typed configuration defaults are less error-prone than shell-only formats

### 3.2 Catalog storage model

In `v0.2.0`, the catalog is defined as **one TOML file per simulator entry**.

Canonical file extension:

```text
*.toml
```

Canonical simulator name:

- lowercase ASCII
- letters, digits, and `-`
- unique after precedence resolution

Example file name:

```text
loopback.toml
```

### 3.3 Discovery paths and precedence

`virtrtlabctl` searches simulator definitions in the following precedence order.
Later entries override earlier ones when the simulator name is identical.

| Precedence | Path | Purpose |
|---|---|---|
| 1 | `/usr/share/virtrtlab/simulators.d/` | built-in catalog shipped by VirtRTLab |
| 2 | `/etc/virtrtlab/simulators.d/` | system-wide administrator overrides |
| 3 | `$XDG_CONFIG_HOME/virtrtlab/simulators.d/` | per-user custom simulators |
| 4 | `~/.config/virtrtlab/simulators.d/` | fallback when `XDG_CONFIG_HOME` is unset |
| 5 | `./.virtrtlab/simulators.d/` | workspace-local catalog versioned with an AUT or validation project |

If two entries define the same `name`, the highest-precedence file wins.

Override behaviour must be explicit in CLI output, for example during `virtrtlabctl sim list --verbose` or equivalent diagnostics.

### 3.4 Catalog entry schema

Each simulator file describes exactly one simulator entry.

Required top-level keys:

| Key | Type | Description |
|---|---|---|
| `api_version` | integer | Catalog format version. Initial value: `1`. |
| `name` | string | Stable simulator name used by CLI and profiles. |
| `summary` | string | One-line human-readable description. |
| `exec` | array of strings | Command vector executed by `virtrtlabctl` (`execve` style). |
| `supports` | array of strings | Supported VirtRTLab device kinds such as `uart`, later `spi` or `i2c`. |

Optional top-level keys:

| Key | Type | Description |
|---|---|---|
| `description` | string | Longer help text shown by inspect commands. |
| `cwd` | string | Working directory for the simulator process. |
| `args` | array of strings | Static arguments appended after `exec`. |
| `default_auto_start` | bool | Hint for profile-driven startup. Default `false`. |
| `restart_policy` | string | Lifecycle hint. Allowed initial values: `never`, `on-failure`. |

Optional parameter declarations:

```toml
[[parameters]]
name = "delay_ms"
type = "u32"
required = false
default = 0
description = "Artificial loopback delay in milliseconds"
```

Parameter declaration fields:

| Field | Type | Description |
|---|---|---|
| `name` | string | Stable parameter name used in attachments and profiles. |
| `type` | string | Declared type. Initial set: `string`, `bool`, `u32`, `u64`. |
| `required` | bool | Whether the parameter must be provided by the attachment. |
| `default` | scalar | Default value when not overridden. |
| `description` | string | User-facing explanation. |

### 3.5 Example catalog entry

```toml
api_version = 1
name = "loopback"
summary = "Echo bytes back to the same VirtRTLab point-to-point link"
description = "Reference simulator used for smoke tests and third-party contract validation"
exec = ["/usr/libexec/virtrtlab/sim-loopback"]
supports = ["uart"]
args = []
default_auto_start = false
restart_policy = "never"

[[parameters]]
name = "delay_ms"
type = "u32"
required = false
default = 0
description = "Delay before echoing data back"
```

---

## 4. Attachment model

### 4.1 Attachment concept

An attachment binds one VirtRTLab device instance to one simulator catalog entry.

The simulator catalog describes a simulator *class*.
An attachment describes one concrete *use* of that simulator class for one concrete VirtRTLab device.

An attachment therefore answers:

- which VirtRTLab device is targeted
- which simulator from the catalog is attached to it
- which configuration values apply to that simulator instance
- whether that simulator should be started automatically with the lab

Examples:

- `uart0` → `loopback`
- `uart1` → `ublox-m8-nmea`
- future `spi0` → `mcp3008`

In `v0.2.0`, the attachment model is intentionally simple:

- one device has at most one managed simulator attachment
- one managed simulator attachment targets exactly one VirtRTLab device
- no fan-out, tap, or observer graph is defined in `v0.2.0`

This matches the existing single-active-connection model on `/run/virtrtlab/<device>.sock`.

### 4.2 Attachment states

An attachment has the following conceptual states:

| State | Meaning |
|---|---|
| `detached` | no simulator is associated with the device |
| `attached` | simulator is selected but not running |
| `starting` | `virtrtlabctl` is launching the simulator |
| `running` | simulator process exists and has not exited; in `v0.2.0` this is a liveness state, not a readiness guarantee |
| `stopping` | `virtrtlabctl` has requested termination and is waiting for process exit |
| `failed` | launch failed or the simulator exited unexpectedly |
| `stopped` | simulator was previously running and has been stopped cleanly |

### 4.3 Attachment declaration in lab profiles

Lab profiles remain TOML.

Simulator attachments are declared separately from `[[devices]]`.

Canonical `v0.2.0` shape:

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

Attachment fields:

| Field | Type | Description |
|---|---|---|
| `device` | string | VirtRTLab device name such as `uart0`. |
| `simulator` | string | Catalog name such as `loopback`. |
| `auto_start` | bool | Whether `virtrtlabctl up --config ...` starts it automatically. Default `false`. |
| `config` | table | Parameter overrides for the selected simulator. Scalars, arrays, and nested tables are allowed. |

Validation rules:

- `device` must exist in the resolved device set
- `simulator` must exist in the resolved catalog
- the simulator must support the target device kind
- unknown `config` keys are rejected
- missing required parameters are rejected

Profile validation must fail before partial startup if any attachment is invalid.

`attachments.config` is the instance-level configuration surface of the simulator.
It may carry simple scalar tuning values or structured configuration such as short waypoint lists, framed responses, or nested behaviour settings.

However, large reusable validation scenarios should preferably be stored in dedicated files referenced from `attachments.config`, rather than embedded inline in the lab profile.

Recommended pattern for large scenarios:

```toml
[[attachments]]
device = "uart0"
simulator = "ublox"
auto_start = true

[attachments.config]
scenario_file = "./scenarios/urban-drive.toml"
```

### 4.4 Attachment lifecycle and teardown

Lifecycle expectations in `v0.2.0`:

- `virtrtlabctl up --config` may create and auto-start declared attachments
- only attachments with `auto_start = true` are started automatically by default
- `virtrtlabctl down` stops managed simulators before tearing down kernel modules
- if a simulator exits unexpectedly, the attachment state becomes `failed`
- `restart_policy = "never"` means `virtrtlabctl` records failure and does not restart automatically
- `restart_policy = "on-failure"` means a later revision may restart automatically; the contract reserves the value now but does not require immediate implementation sophistication

Readiness semantics in `v0.2.0` are intentionally minimal:

- no active readiness probe is required by the base contract
- `running` means the process is alive, not that it has completed every simulator-specific initialization step
- AUT test suites may add a short startup delay or perform simulator-specific checks before launching the AUT
- simulator authors should fail fast during initialization rather than stay alive in a broken or unusable state

Bus reset semantics:

- `vrtlbus0 state=reset` does **not** imply that `virtrtlabctl` must kill the simulator process
- managed simulators must tolerate daemon disconnect and later re-connection when the runtime socket becomes usable again

This preserves the existing daemon model and avoids conflating bus reset with process supervision.

---

## 5. Runtime contract: environment and arguments

### 5.1 Principle

VirtRTLab passes runtime context to simulator processes primarily through **environment variables**.

The command line is kept stable and explicit:

- `exec` and `args` come from the catalog entry
- `virtrtlabctl` does **not** invent hidden positional arguments
- the resolved attachment configuration is written to a file and exposed through an environment variable

This keeps third-party simulators shell-agnostic and avoids a fragile placeholder-expansion mini-language in `v0.2.0`.

### 5.2 Required environment variables

| Variable | Type | Description |
|---|---|---|
| `VIRTRTLAB_SIM_NAME` | string | Catalog simulator name, e.g. `loopback`. |
| `VIRTRTLAB_SIM_INSTANCE_ID` | string | Runtime-unique attachment instance identifier. |
| `VIRTRTLAB_SIM_DEVICE` | string | Target device name, e.g. `uart0`. |
| `VIRTRTLAB_SIM_DEVICE_TYPE` | string | Device kind, e.g. `uart`. |
| `VIRTRTLAB_SIM_SOCKET` | absolute path | Runtime socket path to connect to, e.g. `/run/virtrtlab/uart0.sock`. |
| `VIRTRTLAB_SIM_RUN_DIR` | absolute path | VirtRTLab runtime directory, e.g. `/run/virtrtlab`. |
| `VIRTRTLAB_SIM_CONTROL_DIR` | absolute path | Sysfs control root for the target device when available, e.g. `/sys/kernel/virtrtlab/devices/uart0`. |
| `VIRTRTLAB_SIM_CONFIG` | absolute path | Resolved attachment configuration file written by `virtrtlabctl`. |
| `VIRTRTLAB_SIM_LOG_DIR` | absolute path | Directory reserved for simulator logs and state files. |

Optional variables reserved for later use:

| Variable | Description |
|---|---|
| `VIRTRTLAB_SIM_PROFILE` | Source lab profile path when startup came from a profile. |
| `VIRTRTLAB_SIM_CATALOG_FILE` | Catalog file path that defined the selected simulator. |

### 5.3 Command line contract

The effective command line is:

```text
<exec[0]> <exec[1..]> <args[0..]>
```

Rules:

- `exec` is required and must not be empty
- `args` is optional and may be empty
- `virtrtlabctl` does not append implicit `--socket`, `--device`, or positional arguments in `v0.2.0`
- simulator authors must read runtime context from environment variables and the config file referenced by `VIRTRTLAB_SIM_CONFIG`

### 5.4 Resolved configuration file

Before launching a simulator, `virtrtlabctl` writes the fully resolved attachment configuration to a TOML file.

The file includes:

- simulator name
- target device name and type
- effective parameter values after defaults and profile overrides
- selected restart policy

Illustrative shape:

```toml
simulator = "loopback"
device = "uart0"
device_type = "uart"
restart_policy = "never"

[config]
delay_ms = 0
```

The file is passed to the simulator via:

```text
VIRTRTLAB_SIM_CONFIG=/run/virtrtlab/simulators/uart0/config.toml
```

### 5.5 Process ownership

Managed simulators are ordinary userspace processes.

In `v0.2.0`, the intended execution model is:

- the simulator runs as the invoking user when that user already has access to `/run/virtrtlab/*.sock`
- the simulator must not require root by default
- access to the runtime socket is granted by the existing `virtrtlab` group model

If a simulator requires additional privileges beyond socket access, that requirement belongs to the simulator implementation and must be documented by that simulator, not silently assumed by VirtRTLab.

### 5.6 Runtime state layout

Managed simulator state lives under:

```text
/run/virtrtlab/simulators/
```

The directory layout is per attached device.

Canonical `v0.2.0` layout:

```text
/run/virtrtlab/
└── simulators/
		├── state.json
		├── uart0/
		│   ├── attachment.toml
		│   ├── config.toml
		│   ├── pid
		│   ├── state.json
		│   └── logs/
		│       ├── stdout.log
		│       └── stderr.log
		└── uart1/
				└── ...
```

File semantics:

| Path | Meaning |
|---|---|
| `/run/virtrtlab/simulators/state.json` | aggregate runtime view used by `sim status` without arguments |
| `/run/virtrtlab/simulators/<device>/attachment.toml` | persisted attachment definition resolved from explicit `sim attach` or profile materialization |
| `/run/virtrtlab/simulators/<device>/config.toml` | resolved simulator configuration passed through `VIRTRTLAB_SIM_CONFIG` |
| `/run/virtrtlab/simulators/<device>/pid` | current simulator PID when alive |
| `/run/virtrtlab/simulators/<device>/state.json` | current attachment state, timestamps, last exit code, and catalog source |
| `/run/virtrtlab/simulators/<device>/logs/stdout.log` | captured stdout stream |
| `/run/virtrtlab/simulators/<device>/logs/stderr.log` | captured stderr stream |

Rules:

- `attachment.toml` survives `sim stop` and is removed by `sim detach`
- `config.toml` is regenerated on each `sim start` from catalog defaults plus attachment overrides
- `pid` exists only while a simulator process is believed alive
- `state.json` remains after process exit so failures are inspectable
- `logs/` is created lazily on first start of the attachment
- `virtrtlabctl down` removes runtime-only state for active processes but may preserve attachment state if the command is later specified to support persistent lab attachments; `v0.2.0` initial expectation is full cleanup of process runtime under `/run`

### 5.6.1 Per-device `state.json` format

`/run/virtrtlab/simulators/<device>/state.json` is the normative per-attachment state file.

It is JSON encoded as one object with the following fields:

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | integer | State file schema version. Initial value: `1`. |
| `device` | string | VirtRTLab device name, for example `uart0`. |
| `device_type` | string | Device kind, for example `uart`. |
| `simulator` | string | Catalog simulator name, for example `loopback`. |
| `instance_id` | string | Runtime-unique attachment instance identifier. Changes on each successful `sim attach`. |
| `state` | string enum | One of `attached`, `starting`, `running`, `stopping`, `stopped`, `failed`. |
| `auto_start` | boolean | Effective auto-start policy for the attachment. |
| `restart_policy` | string enum | One of `never`, `on-failure`. |
| `catalog_file` | string | Absolute path to the winning catalog entry file. |
| `config_file` | string | Absolute path to the generated resolved config TOML file. |
| `log_dir` | string | Absolute path to the simulator log directory. |
| `pid` | integer or `null` | Managed simulator process ID while a process is believed alive. `null` otherwise. |
| `last_exit_code` | integer or `null` | Last observed numeric exit status. `null` if the process never reached a terminal state yet or if the stop/crash path has not produced one. |
| `last_error` | string or `null` | Short operator-facing failure reason for `failed` state, for example `execve failed` or `socket connect failed`. |
| `created_at` | string | RFC 3339 timestamp for attachment creation. |
| `started_at` | string or `null` | RFC 3339 timestamp of the latest successful process spawn. |
| `stopped_at` | string or `null` | RFC 3339 timestamp of the latest terminal process transition. |
| `updated_at` | string | RFC 3339 timestamp of the latest state mutation. |

Field rules:

- `schema_version` is required and must be `1` in the initial contract
- `device`, `device_type`, `simulator`, `instance_id`, `state`, `auto_start`, `restart_policy`, `catalog_file`, `config_file`, `log_dir`, `created_at`, and `updated_at` are always present
- `pid` is non-null only in `starting`, `running`, or `stopping`
- `started_at` is non-null only after at least one successful spawn
- `stopped_at` is non-null only after a terminal process state has been observed
- `last_error` should be `null` outside `failed`
- `last_exit_code` should be retained across later `stopped` and `failed` observations until the next successful `sim start`

Canonical example while running:

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

Canonical example after a crash:

```json
{
	"schema_version": 1,
	"device": "uart0",
	"device_type": "uart",
	"simulator": "loopback",
	"instance_id": "uart0-20260322T184200Z-14523",
	"state": "failed",
	"auto_start": true,
	"restart_policy": "never",
	"catalog_file": "/usr/share/virtrtlab/simulators.d/loopback.toml",
	"config_file": "/run/virtrtlab/simulators/uart0/config.toml",
	"log_dir": "/run/virtrtlab/simulators/uart0/logs",
	"pid": null,
	"last_exit_code": 1,
	"last_error": "process exited unexpectedly",
	"created_at": "2026-03-22T18:40:10Z",
	"started_at": "2026-03-22T18:42:00Z",
	"stopped_at": "2026-03-22T18:42:03Z",
	"updated_at": "2026-03-22T18:42:03Z"
}
```

### 5.6.2 Aggregate `state.json` format

`/run/virtrtlab/simulators/state.json` is the aggregate state file used to serve `virtrtlabctl sim status` without a device argument.

It is JSON encoded as:

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

Rules:

- `attachments` contains zero or more summaries, one per attached device
- the file is a convenience view; the per-device file remains the source of truth
- summary objects must contain at least `device`, `state`, `simulator`, `pid`, `auto_start`, and `updated_at`

### 5.6.3 State transitions

The state machine is driven by attachment lifecycle events.

Allowed transitions:

| Current state | Event | Next state | Observable effect |
|---|---|---|---|
| `detached` | `sim attach` succeeds | `attached` | attachment files are created and `state.json` appears |
| `attached` | `sim start` requested | `starting` | PID may be recorded once the child exists; logs/config are prepared |
| `starting` | child remains alive past immediate launch checks | `running` | `started_at` is set, `last_error` cleared |
| `starting` | spawn or early initialization fails | `failed` | `pid` becomes `null`, `last_error` is populated |
| `running` | `sim stop` requested | `stopping` | graceful termination begins |
| `stopping` | process exits after requested stop | `stopped` | `pid` becomes `null`, `stopped_at` and `last_exit_code` are updated |
| `running` | process exits without operator-requested stop | `failed` | treated as crash or unexpected exit |
| `failed` | `sim start` requested again and succeeds | `starting` then `running` | previous failure is retained until successful restart clears `last_error` |
| `stopped` | `sim start` requested | `starting` | same behaviour as first start |
| `attached` | `sim detach` succeeds | `detached` | per-device runtime directory is removed |
| `stopped` | `sim detach` succeeds | `detached` | per-device runtime directory is removed |
| `failed` | `sim detach` succeeds | `detached` | failure state is discarded with the attachment |

Disallowed transitions and CLI outcomes:

| Current state | Event | Result |
|---|---|---|
| `running` | `sim start` | state conflict error |
| `starting` | `sim start` | state conflict error |
| `attached` | `sim stop` | operational error because no process exists |
| `failed` | `sim stop` | operational error because no live process exists |
| `detached` | `sim start` | not-found error because no attachment exists |
| `detached` | `sim detach` | not-found error because no attachment exists |

Crash semantics:

- an unexpected process exit from `running` always transitions to `failed`
- a launch-time failure from `starting` also transitions to `failed`
- `failed` means the last lifecycle attempt did not end in a clean operator-requested stop
- in `v0.2.0`, `restart_policy = "on-failure"` does not require automatic restart; the file still records the intended policy for later revisions

`virtrtlabctl down` semantics:

- if an attachment is `running`, `starting`, or `stopping`, `virtrtlabctl down` must attempt a stop sequence before removing runtime state
- after successful teardown, `/run/virtrtlab/simulators/` may be removed entirely
- because `down` tears down the whole lab, no final persistent state file is required to survive that cleanup in `v0.2.0`

---

## 6. Reference loopback simulator

`loopback` is the mandatory built-in simulator for `v0.2.0`.

It is both:

- the first operational simulator shipped with VirtRTLab
- the reference implementation of the simulator contract for third-party authors

### 6.1 Scope

In `v0.2.0`, `loopback` supports only `uart` attachments.

It connects to the runtime socket specified by `VIRTRTLAB_SIM_SOCKET` and echoes received bytes back to the same link.

### 6.2 Required behaviour

The `loopback` simulator shall:

- connect to the target UART socket
- read raw bytes continuously
- write the same bytes back in-order to the same socket
- preserve byte values exactly unless a configured loopback-specific option says otherwise
- exit non-zero on startup failure such as socket-connection failure or invalid configuration

### 6.3 Optional parameters

The initial built-in parameter set is:

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `delay_ms` | `u32` | `0` | Artificial delay before echoing a received chunk |

### 6.4 Logging expectations

The reference `loopback` implementation should be quiet by default.

Expected log behaviour:

- startup success may log one concise line to stdout or stderr
- repeated per-byte logging is forbidden by default
- fatal startup or runtime errors must be logged to stderr before exit

### 6.5 Test value

`loopback` exists primarily to support:

- smoke tests for the end-to-end UART path
- validation of attachment, start, stop, and logs lifecycle
- validation of the simulator catalog and config plumbing
- a minimal example for third-party simulator authors

### 6.6 Example built-in entry

```toml
api_version = 1
name = "loopback"
summary = "Echo bytes back to the same VirtRTLab point-to-point link"
description = "Reference simulator used for smoke tests and contract validation"
exec = ["/usr/libexec/virtrtlab/sim-loopback"]
supports = ["uart"]
restart_policy = "never"

[[parameters]]
name = "delay_ms"
type = "u32"
required = false
default = 0
description = "Delay before echoing received bytes back to the AUT"
```

---

## 7. CLI surface

The simulator contract depends on a concrete `virtrtlabctl sim` command family.
The command names below are part of the `v0.2.0` contract draft.

### 7.1 Command set

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

### 7.2 `sim list`

Lists all catalog entries visible after precedence resolution.

- default output shows `name`, supported device kinds, and one-line summary
- `--type TYPE` filters entries by supported device kind such as `uart`
- `--verbose` additionally shows the catalog source file and whether the entry overrides a lower-precedence definition

### 7.3 `sim inspect`

Shows the full metadata for one simulator entry.

The output includes at least:

- simulator name
- summary and description
- supported device kinds
- catalog source file
- declared parameters and defaults
- restart policy

### 7.4 `sim attach`

Creates or replaces the attachment definition for one device.

Arguments:

- `<device>` is the VirtRTLab device name such as `uart0`
- `<simulator>` is a catalog name such as `loopback`

Options:

- `--auto-start` sets the attachment `auto_start` flag to `true`
- `--set key=value` provides per-attachment configuration overrides; option may be repeated

Behaviour:

- validates that the device exists
- validates that the simulator exists and supports the device kind
- validates all provided config keys against declared parameters
- writes the resolved attachment state into the VirtRTLab runtime state directory
- does **not** start the simulator process implicitly; use `sim start` or profile-driven startup

### 7.5 `sim detach`

Removes the attachment definition for one device.

Behaviour:

- if a simulator is currently running for the device, `sim detach` stops it first
- attachment state and generated runtime config files are removed afterwards
- detaching an already detached device returns a not-found style error

### 7.6 `sim start`

Starts the simulator attached to one device.

Behaviour:

- requires an existing attachment
- fails if the attachment is already in `starting` or `running`
- launches the simulator using the catalog `exec` and `args`
- provides runtime context exclusively through the environment variables and config file defined in this document
- marks the attachment `running` once the process has been successfully spawned and has not exited immediately

In `v0.2.0`, `sim start` does not wait for a simulator-specific readiness probe.

### 7.7 `sim stop`

Stops the simulator attached to one device.

Behaviour:

- if the attachment is `running`, sends a graceful termination signal first
- if the process does not exit within a bounded timeout, force-kills it
- leaves the attachment definition in place and moves the state to `stopped`
- stopping an attachment that is not running is a no-op success only when the state is already `stopped`; otherwise it is an error for a missing attachment

### 7.8 `sim status`

Displays runtime status for one attachment or all attachments.

Without argument:

- prints one row or JSON object per attached device

With `<device>`:

- prints detailed status for the specified device only

Status payload includes at least:

- device name
- simulator name
- attachment state
- PID if a simulator process is currently alive
- `auto_start` flag
- runtime config path
- log directory path

### 7.9 `sim logs`

Shows logs for the simulator attached to one device.

Default behaviour:

- prints stdout log content for the attachment

Options:

- `--stderr` selects stderr log content instead of stdout
- `--tail N` prints only the last `N` lines
- `--follow` keeps streaming appended lines, similar to `tail -f`

This command reads logs from the managed attachment log directory; it does not attach to the live process stream interactively.

`--tail N --follow` is valid and means: print the last `N` lines, then keep following the file.

### 7.10 Exit-code expectations

The existing global CLI exit-code model remains in force.

Recommended mapping for simulator commands:

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Operational error (spawn failure, process stop timeout, filesystem error) |
| 2 | Bad arguments or invalid config syntax |
| 3 | State conflict (already running, already attached, already starting) |
| 4 | Not found or incompatible target (unknown simulator, unknown device, unsupported device kind, no attachment) |

---

## 8. Rationale

### Why TOML for the catalog?

VirtRTLab already documents TOML lab profiles. Reusing TOML avoids teaching users a second declarative format for adjacent concepts.

### Why one file per simulator instead of a single catalog database?

Per-file definitions are easier to override, package, and review. They also fit naturally with built-in versus system versus user precedence.

### Why environment variables instead of implicit CLI arguments?

Environment variables give a stable, extensible runtime contract without creating shell-escaping or placeholder-substitution problems. They also make it easier to launch simulators implemented in different languages.

### Why a config file in addition to environment variables?

Environment variables are good for stable runtime context such as device names and paths. Structured simulator configuration is better expressed as a typed TOML file than as dozens of prefixed environment variables.

---

## 9. Open questions

No open questions remain in this first draft of the simulator contract.