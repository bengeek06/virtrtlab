<!-- SPDX-License-Identifier: CC-BY-4.0 -->

# VirtRTLab Simulator Contract (v0.2.0 Draft)

This document is the source of truth for the simulator catalog and lifecycle contract targeted at `v0.2.0`.

It defines how the VirtRTLab control plane discovers simulators, how a
simulator is attached to a VirtRTLab device, and which runtime context is
passed to the simulator process.

In `v0.2.0`, the daemon is the authoritative owner of simulator attachment and
lifecycle state. `virtrtlabctl` remains the normal human-facing client of that
daemon control plane.

This contract belongs to VirtRTLab itself. Protocol-specific simulators such as GPS, sensor, switch-console, or register-oriented component peers may live in separate repositories, but they must conform to the rules defined here if they want to be managed by VirtRTLab.

---

## 1. Scope

The simulator contract exists to make VirtRTLab usable as a complete test bench rather than a raw virtual link.

The contract covers:

- simulator catalog discovery
- user-extensible simulator definitions
- device-to-simulator attachment
- runtime context passed by the daemon-managed simulator lifecycle
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

The VirtRTLab control plane searches simulator definitions in the following precedence order.
Later entries override earlier ones when the simulator name is identical.

| Precedence | Path | Purpose |
|---|---|---|
| 1 | `/usr/share/virtrtlab/simulators.d/` | built-in catalog shipped by VirtRTLab |
| 2 | `/etc/virtrtlab/simulators.d/` | system-wide administrator overrides |
| 3 | `$XDG_CONFIG_HOME/virtrtlab/simulators.d/` | per-user custom simulators |
| 4 | `~/.config/virtrtlab/simulators.d/` | fallback when `XDG_CONFIG_HOME` is unset |
| 5 | `./.virtrtlab/simulators.d/` | workspace-local catalog versioned with an AUT or validation project |

If two entries define the same `(name, version)` tuple, the highest-precedence file wins.

If two entries share the same `name` but different `version` values, they coexist as distinct visible catalog entries.

Override behaviour must be explicit in CLI output, for example during `virtrtlabctl sim list --verbose` or equivalent diagnostics.

### 3.4 Catalog entry schema

Each simulator file describes exactly one simulator entry.

Required top-level keys:

| Key | Type | Description |
|---|---|---|
| `api_version` | integer | Catalog format version. Initial value: `1`. |
| `name` | string | Stable simulator name used by CLI and profiles. |
| `version` | string | Simulator version identifier. Required for validation traceability. |
| `summary` | string | One-line human-readable description. |
| `exec` | array of strings | Command vector executed by the daemon-managed simulator launcher (`execve` style). |
| `supports` | array of strings | Supported VirtRTLab device kinds such as `uart`, later `spi` or `i2c`. |

Optional top-level keys:

| Key | Type | Description |
|---|---|---|
| `description` | string | Longer help text shown by inspect commands. |
| `cwd` | string | Working directory for the simulator process. |
| `args` | array of strings | Static arguments appended after `exec`. |
| `default_auto_start` | bool | Hint for profile-driven startup. Default `false`. |
| `restart_policy` | string | Lifecycle hint. Allowed initial values: `never`, `on-failure`. |

Version semantics:

- `version` identifies the simulator artifact or release, not the catalog format
- `version` is compared as an opaque string in `v0.2.0`
- semantic versioning such as `1.2.0` is recommended but not required by the base contract
- validation tooling may record `name + version` as the simulator identity tuple

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
| `type` | string | Declared type. Initial set: `string`, `bool`, `u32`, `u64`, `array`, `object`. |
| `required` | bool | Whether the parameter must be provided by the attachment. |
| `default` | scalar | Default value when not overridden. |
| `description` | string | User-facing explanation. |

Type semantics:

| Type | Expected value |
|---|---|
| `string` | TOML string / JSON string |
| `bool` | TOML boolean / JSON boolean |
| `u32` | unsigned 32-bit integer |
| `u64` | unsigned 64-bit integer |
| `array` | JSON array or TOML array value |
| `object` | JSON object or TOML table value |

For `array` and `object`, VirtRTLab validates only the top-level kind in `v0.2.0`.
Nested structure remains simulator-defined.

### 3.5 Example catalog entry

```toml
api_version = 1
name = "loopback"
version = "1.0.0"
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

This matches the single-active-connection dataplane model on
`/run/virtrtlab/devices/<device>.sock`.

### 4.2 Attachment states

An attachment has the following conceptual states:

| State | Meaning |
|---|---|
| `detached` | no simulator is associated with the device |
| `attached` | simulator is selected but not running |
| `starting` | the daemon is launching the simulator |
| `running` | simulator process exists and has not exited; in `v0.2.0` this is a liveness state, not a readiness guarantee |
| `stopping` | the daemon has requested termination and is waiting for process exit |
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
| `version` | string | Optional simulator version selector. Required when multiple visible versions share the same simulator name. |
| `auto_start` | bool | Whether `virtrtlabctl up --config ...` starts it automatically. If omitted, the effective default comes from the catalog `default_auto_start`, else falls back to `false`. |
| `config` | table | Parameter overrides for the selected simulator. Scalars, arrays, and nested tables are allowed. |

Validation rules:

- `device` must exist in the resolved device set
- `simulator` must exist in the resolved catalog
- if `version` is present, the `(simulator, version)` pair must exist in the resolved catalog
- if `version` is absent and multiple visible catalog entries share the same simulator name, validation fails with `ambiguous-simulator-version`
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

### 4.3.1 CLI override grammar for `sim attach --set`

`virtrtlabctl sim attach` must support attachment-local overrides from the CLI.

Initial grammar:

```text
--set <path>=<value>
```

Rules for `<path>`:

- it addresses keys under `attachments.config`
- segments are dot-separated, for example `timing.delay_ms` or `scenario.mode`
- each segment uses lowercase ASCII letters, digits, `_`, or `-`
- array indexing syntax such as `points[0]` is not part of the `v0.2.0` contract

Rules for `<value>`:

- scalar parameters may use plain CLI text such as `42`, `true`, or `gps`
- structured values for `array` and `object` parameters must use JSON literals
- when a structured value is provided, the targeted key is replaced atomically
- repeated `--set` for the same path is allowed and the last value wins

Examples:

```sh
virtrtlabctl sim attach uart0 loopback --set delay_ms=5
virtrtlabctl sim attach uart1 gps --set scenario.mode=urban
virtrtlabctl sim attach uart1 gps --set points='[{"lat":48.85,"lon":2.35}]'
virtrtlabctl sim attach uart1 sensor --set profile='{"boot_ms":20,"burst":[1,2,3]}'
```

Validation rules for CLI overrides:

- the first path segment must match a declared top-level parameter name
- if the top-level parameter type is scalar, additional nested segments are rejected
- if the top-level parameter type is `array` or `object`, nested segments are allowed
- unknown top-level parameters are rejected
- numeric overflow for `u32` and `u64` is rejected
- invalid JSON literals for `array` and `object` are rejected
- a nested assignment into a non-object intermediate value is rejected

Merge rules:

- scalar assignment replaces the target scalar value
- object assignment with a JSON object replaces the target object value at that path
- array assignment with a JSON array replaces the target array value at that path
- dotted nested assignment updates only the addressed subtree and preserves sibling keys

Illustrative mapping:

```sh
virtrtlabctl sim attach uart1 gps \
	--set profile='{"boot_ms":20,"scenario":{"mode":"static"}}' \
	--set profile.scenario.mode=urban
```

Resolved effective config:

```toml
[config.profile]
boot_ms = 20

[config.profile.scenario]
mode = "urban"
```

### 4.3.2 Simulator version selection

Simulator selection is version-aware in `v0.2.0`.

Resolution rules:

- the catalog identity key is `(name, version)`
- precedence resolves collisions only for identical `(name, version)` tuples
- attachments and `sim attach` may omit `version` only when exactly one visible catalog entry exists for the given simulator `name`
- if multiple visible versions exist and no `version` is provided, the command or profile validation must fail and require explicit version selection
- if `version` is provided and no exact match exists, the result is not found

Canonical profile example with explicit version pinning:

```toml
[[attachments]]
device = "uart0"
simulator = "loopback"
version = "1.0.0"
auto_start = true

[attachments.config]
delay_ms = 0
```

### 4.4 Attachment lifecycle and teardown

Lifecycle expectations in `v0.2.0`:

- `virtrtlabctl up --config` may request that the daemon create and auto-start declared attachments
- only attachments with `auto_start = true` are started automatically by default
- `virtrtlabctl down` requests that the daemon stop managed simulators before tearing down the active lab topology
- if a simulator exits unexpectedly, the attachment state becomes `failed`
- `restart_policy = "never"` means the daemon records failure and does not restart automatically
- `restart_policy = "on-failure"` means a later revision may restart automatically; the contract reserves the value now but does not require immediate implementation sophistication

`restart_policy` semantics in `v0.2.0`:

- `restart_policy` is recorded in catalog metadata, resolved config, and `state.json`
- no background supervisor loop is required in `v0.2.0`
- `on-failure` does not imply automatic restart after crash, launch failure, `down`, or reboot
- the only guaranteed recovery path in `v0.2.0` is an explicit later command such as `sim start <device>` or a new `up --config`
- future revisions may strengthen `on-failure`, but `v0.2.0` scripts must not assume autonomous restart behaviour

`default_auto_start` semantics in `v0.2.0`:

- `default_auto_start` is consulted only when an attachment is materialized from a profile and the attachment omits `auto_start`
- direct `sim attach` defaults to `auto_start = false` unless the operator explicitly requests auto-start
- an attachment-local `auto_start` value always overrides the catalog `default_auto_start`

Readiness semantics in `v0.2.0` are intentionally minimal:

- no active readiness probe is required by the base contract
- `running` means the process is alive, not that it has completed every simulator-specific initialization step
- AUT test suites may add a short startup delay or perform simulator-specific checks before launching the AUT
- simulator authors should fail fast during initialization rather than stay alive in a broken or unusable state

Bus reset semantics:

- `vrtlbus0 state=reset` does **not** imply that `virtrtlabctl` must kill the simulator process
- managed simulators must tolerate daemon disconnect and later re-connection when the runtime socket becomes usable again

This preserves the existing daemon model and avoids conflating bus reset with process supervision.

Path-discovery rule:

- simulator processes must use the resolved runtime paths passed by environment variables such as `VIRTRTLAB_SIM_SOCKET`, `VIRTRTLAB_SIM_CONFIG`, and `VIRTRTLAB_SIM_LOG_DIR`
- examples under `/run/virtrtlab/...` are the default installed layout, not the only valid deployed layout

### 4.5 Profile-driven startup failure semantics

`virtrtlabctl up --config <file>` may materialize multiple attachments from one lab profile.

In `v0.2.0`, startup is split conceptually into two phases:

1. profile validation and attachment materialization
2. auto-start of attachments with `auto_start = true`

Rules:

- profile validation failures must abort before partial simulator startup
- invalid attachment declarations must prevent any managed simulator process from being launched
- once validation succeeds, attachment definitions may be materialized for all declared attachments before any auto-start begins
- auto-start is then attempted attachment by attachment

Partial failure semantics during auto-start:

- if one auto-start attachment fails, `up --config` returns a non-zero exit code
- attachments already started successfully before the failure are not rolled back automatically in `v0.2.0`
- attachments not yet started remain in `attached` state
- the failed attachment enters `failed` state with the normal `state.json` failure fields populated
- the command output must make the partial-success situation explicit

Rationale:

- avoiding rollback keeps the contract simple and observable
- successful simulator processes remain inspectable through `sim status` and `sim logs`
- test harnesses can decide whether to call `down`, retry specific attachments, or continue diagnosis in-place

This means `up --config` is atomic for validation, but not atomic for the start of all auto-start attachments.

---

## 5. Runtime contract: environment and arguments

### 5.1 Principle

VirtRTLab passes runtime context to simulator processes primarily through **environment variables**.

The command line is kept stable and explicit:

- `exec` and `args` come from the catalog entry
- `virtrtlabctl` does **not** invent hidden positional arguments
- the resolved attachment configuration is written by the daemon to a file and exposed through an environment variable

This keeps third-party simulators shell-agnostic and avoids a fragile placeholder-expansion mini-language in `v0.2.0`.

### 5.2 Required environment variables

| Variable | Type | Description |
|---|---|---|
| `VIRTRTLAB_SIM_NAME` | string | Catalog simulator name, e.g. `loopback`. |
| `VIRTRTLAB_SIM_INSTANCE_ID` | string | Runtime-unique attachment instance identifier. |
| `VIRTRTLAB_SIM_DEVICE` | string | Target device name, e.g. `uart0`. |
| `VIRTRTLAB_SIM_DEVICE_TYPE` | string | Device kind, e.g. `uart`. |
| `VIRTRTLAB_SIM_SOCKET` | absolute path | Runtime dataplane socket path to connect to, e.g. `/run/virtrtlab/devices/uart0.sock`. |
| `VIRTRTLAB_SIM_RUN_DIR` | absolute path | VirtRTLab runtime directory, e.g. `/run/virtrtlab`. |
| `VIRTRTLAB_SIM_CONTROL_DIR` | absolute path | Sysfs control root for the target device when available, e.g. `/sys/kernel/virtrtlab/devices/uart0`. |
| `VIRTRTLAB_SIM_CONFIG` | absolute path | Resolved attachment configuration file written by the daemon. |
| `VIRTRTLAB_SIM_LOG_DIR` | absolute path | Directory reserved for simulator log files (e.g. `stdout.log`, `stderr.log`). |

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
- the daemon-managed launcher does not append implicit `--socket`, `--device`, or positional arguments in `v0.2.0`
- simulator authors must read runtime context from environment variables and the config file referenced by `VIRTRTLAB_SIM_CONFIG`

### 5.4 Resolved configuration file

Before launching a simulator, the daemon writes the fully resolved attachment configuration to a TOML file.

The file includes:

- simulator name
- simulator version
- target device name and type
- effective parameter values after defaults and profile overrides
- selected restart policy

Illustrative shape:

```toml
simulator = "loopback"
simulator_version = "1.0.0"
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

- the simulator runs under the daemon-managed service context in the normal installed profile
- the simulator must not require root by default
- access to the runtime socket is granted by the existing `virtrtlab` group model

If a simulator requires additional privileges beyond socket access, that requirement belongs to the simulator implementation and must be documented by that simulator, not silently assumed by VirtRTLab.

### 5.6 Runtime state layout

Managed simulator state lives under:

```text
/run/virtrtlab/simulators/
```

This path is the default installed simulator runtime root. The resolved runtime
root comes from daemon configuration and remains the value surfaced through the
environment and control-plane-visible paths.

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
- the daemon owns creation, mutation, and cleanup of this runtime state
- `virtrtlabctl down` requests daemon-side cleanup of runtime-only state for active processes; `v0.2.0` initial expectation is full cleanup of process runtime under `/run`
- these files are a normative simulator-runtime observability surface only; they do not replace the control socket as the topology-control API

### 5.6.1 Per-device `state.json` format

`/run/virtrtlab/simulators/<device>/state.json` is the normative per-attachment state file.

It is JSON encoded as one object with the following fields:

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | integer | State file schema version. Initial value: `1`. |
| `device` | string | VirtRTLab device name, for example `uart0`. |
| `device_type` | string | Device kind, for example `uart`. |
| `simulator` | string | Catalog simulator name, for example `loopback`. |
| `simulator_version` | string | Catalog simulator version string, for example `1.0.0`. |
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
- `device`, `device_type`, `simulator`, `simulator_version`, `instance_id`, `state`, `auto_start`, `restart_policy`, `catalog_file`, `config_file`, `log_dir`, `created_at`, and `updated_at` are always present
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

Canonical example after a crash:

```json
{
	"schema_version": 1,
	"device": "uart0",
	"device_type": "uart",
	"simulator": "loopback",
	"simulator_version": "1.0.0",
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
			"simulator_version": "1.0.0",
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
- summary objects must contain at least `device`, `state`, `simulator`, `simulator_version`, `pid`, `auto_start`, and `updated_at`

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
| `attached` | `sim stop` | state conflict error because no process is running |
| `failed` | `sim stop` | state conflict error because no process is running |
| `detached` | `sim start` | `not-attached` control-plane error, mapped to CLI exit code `4` |
| `detached` | `sim detach` | `not-attached` control-plane error, mapped to CLI exit code `4` |

Crash semantics:

- an unexpected process exit from `running` always transitions to `failed`
- a launch-time failure from `starting` also transitions to `failed`
- `failed` means the last lifecycle attempt did not end in a clean operator-requested stop
- in `v0.2.0`, `restart_policy = "on-failure"` does not require automatic restart; the file still records the intended policy for later revisions

Observable `restart_policy` consequence in `v0.2.0`:

- after a crash, the attachment remains in `failed` until an operator-driven action occurs
- no implicit transition `failed -> starting` may happen solely because `restart_policy = "on-failure"`

`virtrtlabctl down` semantics:

- if an attachment is `running`, `starting`, or `stopping`, `virtrtlabctl down` must attempt a stop sequence before removing runtime state
- after successful teardown, `/run/virtrtlab/simulators/` may be removed entirely
- because `down` tears down the whole lab, no final persistent state file is required to survive that cleanup in `v0.2.0`

### 5.6.4 Atomicity and locking

Managed simulator state must be safe against concurrent CLI invocations.

Initial `v0.2.0` contract:

- one per-device lock protects `/run/virtrtlab/simulators/<device>/`
- one aggregate lock protects `/run/virtrtlab/simulators/state.json`
- `virtrtlabctl` must serialize conflicting lifecycle mutations on the same device
- non-conflicting operations on different devices may proceed independently

Locking intent:

| Scope | Protected operations |
|---|---|
| per-device lock | `sim attach`, `sim detach`, `sim start`, `sim stop`, per-device state updates |
| aggregate lock | aggregate state regeneration for `sim status` without device argument |
| global lab teardown | `down`, and the simulator-related part of `up --config`, while lab-wide cleanup or materialization is running |

Atomic update rules:

- `state.json`, `attachment.toml`, and `config.toml` must be written by create-temp-and-rename semantics
- `pid` may be written as a simple replacement file, but stale `pid` content must never survive a terminal state transition
- aggregate `state.json` must be regenerated from per-device source-of-truth files, never edited in place by partial string mutation
- `sim status` must observe either the old complete state or the new complete state, never a truncated file

Failure handling rules:

- if a process spawn succeeds but a later state-file update fails, the command must attempt best-effort process termination before returning an operational error
- if lock acquisition fails due to timeout or unrecoverable I/O, the command returns exit code `1`
- a stale lock caused by a dead process is an implementation concern; the observable contract is only that commands must not hang indefinitely

### 5.6.5 Concurrent command semantics

Concurrent lifecycle requests targeting the same device are serialized by the per-device lock.

Observable outcomes:

| Situation | Expected result |
|---|---|
| two concurrent `sim start uart0` | one succeeds, the other returns state conflict or sees already-running state |
| `sim stop uart0` racing with `sim start uart0` | whichever acquires the device lock first completes first; the second evaluates the new post-lock state |
| `sim attach uart0 ...` racing with `sim detach uart0` | serialized; final state matches command completion order |
| `sim status uart0` during `sim start uart0` | may report `attached`, `starting`, or `running`, but must return a coherent single state object |
| `sim status` during updates on multiple devices | aggregate output may reflect some devices before and others after independent operations, but each per-device object must be coherent |
| `down` racing with any `sim * <device>` mutation | `down` wins once it acquires the relevant lock scope; later commands fail with operational, `not-attached`, or `unknown-device` outcomes depending on the resulting lab state |

The contract does not require fairness beyond eventual completion.
It does require bounded waiting or explicit failure rather than indefinite blocking.

Recommended timeout guidance:

- per-device lifecycle lock acquisition should fail in a bounded time on the order of seconds, not minutes
- `sim status` should avoid waiting behind long process-stop paths when a coherent previous state can still be returned

### 5.6.6 Persistence and cleanup semantics

Simulator runtime state under `/run/virtrtlab/simulators/` is ephemeral.

In `v0.2.0`, VirtRTLab distinguishes between:

- attachment intent that exists only while the current lab runtime exists
- durable simulator catalog definitions stored outside `/run`

The initial persistence contract is intentionally simple:

- attachment definitions created by `sim attach` are runtime-local, not reboot-persistent
- `state.json`, `attachment.toml`, `config.toml`, `pid`, and `logs/` are all disposable runtime artifacts
- after reboot or any loss of `/run/virtrtlab/simulators/`, no attachment is considered to exist anymore unless it is recreated by `sim attach` or `up --config`
- no daemon or CLI client may treat these runtime files as a durable substitute for control-plane topology state

Observable semantics by event:

| Event | Expected post-condition |
|---|---|
| `sim detach <device>` succeeds | `/run/virtrtlab/simulators/<device>/` is removed entirely |
| `sim stop <device>` succeeds | attachment directory remains, with `state=stopped` and no `pid` |
| `virtrtlabctl down` succeeds | `/run/virtrtlab/simulators/` may be removed entirely |
| host reboot | `/run/virtrtlab/simulators/` is assumed lost; all attachments are therefore forgotten |
| manual deletion of one per-device directory | that attachment is treated as detached on the next CLI observation |
| manual deletion of aggregate `state.json` only | aggregate view is regenerated from per-device state on the next relevant command |
| manual deletion of `pid` only while process still lives | implementation may reconstitute state by process checks or mark failure; it must not report a fake running PID |

Rules for commands after runtime loss:

- `sim status` with no existing runtime directory returns an empty attachment set, not an operational error
- `sim status <device>` returns a no-attachment condition for an existing device and a not-found condition only when the device itself is absent
- `sim start <device>` returns a no-attachment condition when the attachment runtime directory is absent, even if a simulator process somehow still exists outside management
- `sim detach <device>` returns a no-attachment condition when no managed attachment runtime state exists

Relationship with profiles:

- profile files are the durable way to recreate attachments across reboots or fresh lab startups
- `virtrtlabctl up --config <file>` may materialize attachments again from the profile regardless of previous `/run` contents
- runtime attachment state must never be treated as a durable substitute for the lab profile

Log retention rules:

- logs under `/run/virtrtlab/simulators/<device>/logs/` are best-effort diagnostic artifacts only
- `sim detach` removes them with the rest of the per-device runtime directory
- `down` may remove all runtime logs as part of lab teardown
- users who need durable logs must export or copy them before teardown

---

## 6. Reference loopback simulator

`loopback` is the mandatory built-in simulator for `v0.2.0`.

It is both:

- the first operational simulator shipped with VirtRTLab
- the reference implementation of the simulator contract for third-party authors

### 6.1 Scope

In `v0.2.0`, `loopback` supports only `uart` attachments.

It connects to the runtime dataplane socket specified by `VIRTRTLAB_SIM_SOCKET` and echoes received bytes back to the same link.

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
version = "1.0.0"
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

### 6.7 Reference CI stub simulator

`test-stub` is the reference deterministic simulator intended for CLI, lifecycle, and CI validation.

Unlike `loopback`, it is not meant to model a realistic peripheral protocol.
Its role is to make simulator process behaviour easy to control from tests.

#### Scope

In `v0.2.0`, `test-stub` may support `uart` attachments only.

It should still connect to `VIRTRTLAB_SIM_SOCKET` when operating in steady-state `run` mode so that attachment startup exercises the same dataplane-socket path contract as ordinary simulators.

#### Required behaviour

The reference `test-stub` simulator shall:

- accept the same environment/config contract as any other managed simulator
- optionally emit one configured line to stdout and one configured line to stderr during startup
- optionally delay startup by a bounded number of milliseconds
- either stay alive, exit immediately with a configured code, or crash after a bounded runtime depending on configuration
- terminate cleanly on `SIGTERM` by default

#### Initial parameter set

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `mode` | `string` | `run` | One of `run`, `fail`, `crash` |
| `startup_delay_ms` | `u32` | `0` | Delay before the simulator decides whether to run or fail |
| `runtime_ms` | `u32` | `0` | In `run` or `crash` mode, `0` means run until signaled; non-zero means exit after the delay |
| `exit_code` | `u32` | `1` | Exit status used in `fail` or timed `crash` mode |
| `stdout_line` | `string` | `""` | Optional startup line written once to stdout |
| `stderr_line` | `string` | `""` | Optional startup line written once to stderr |
| `ignore_sigterm` | `bool` | `false` | When `true`, ignore the first graceful stop signal so tests can exercise force-kill fallback |

Mode semantics:

| Mode | Expected behaviour |
|---|---|
| `run` | connect to the socket, emit configured logs, and stay alive until signaled or until `runtime_ms` elapses |
| `fail` | remain in `starting` during `startup_delay_ms`, then emit configured logs and exit non-zero before entering `running` |
| `crash` | remain in `starting` during `startup_delay_ms`, enter `running`, then exit with `exit_code` after `runtime_ms` or a minimal bounded post-start delay |

Determinism rules for lifecycle tests:

- `startup_delay_ms` elapses entirely while the attachment remains in `starting`
- `mode=fail` must never yield a durable observable `running` state
- `mode=crash` must yield an observable `running` state before transitioning to `failed`
- if `mode=crash` and `runtime_ms=0`, the implementation must still allow one bounded post-start interval so `running` is observable at least once

#### Test value

`test-stub` exists primarily to support:

- deterministic `starting -> running` and `starting -> failed` coverage
- deterministic `running -> failed` crash coverage
- stdout/stderr log-capture validation
- stop-timeout and force-kill testing via `ignore_sigterm`
- partial-startup profile tests without relying on protocol-specific simulators

#### Example built-in entry

```toml
api_version = 1
name = "test-stub"
version = "1.0.0"
summary = "Deterministic simulator process for VirtRTLab CI and CLI validation"
description = "Reference stub used to exercise lifecycle, logs, and error handling without protocol realism"
exec = ["/usr/libexec/virtrtlab/sim-test-stub"]
supports = ["uart"]
restart_policy = "never"

[[parameters]]
name = "mode"
type = "string"
required = false
default = "run"
description = "Lifecycle mode: run, fail, or crash"

[[parameters]]
name = "startup_delay_ms"
type = "u32"
required = false
default = 0
description = "Delay before startup succeeds or fails"

[[parameters]]
name = "runtime_ms"
type = "u32"
required = false
default = 0
description = "Runtime duration before timed exit; 0 means run until signaled"

[[parameters]]
name = "exit_code"
type = "u32"
required = false
default = 1
description = "Exit code used in fail or crash mode"

[[parameters]]
name = "stdout_line"
type = "string"
required = false
default = ""
description = "Optional startup line written once to stdout"

[[parameters]]
name = "stderr_line"
type = "string"
required = false
default = ""
description = "Optional startup line written once to stderr"

[[parameters]]
name = "ignore_sigterm"
type = "bool"
required = false
default = false
description = "Ignore the first graceful stop signal so tests can exercise force-kill fallback"
```

---

## 7. CLI surface

The simulator contract depends on a concrete `virtrtlabctl sim` command family.
The command names below are part of the `v0.2.0` contract draft.

### 7.1 Command set

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

### 7.2 `sim list`

Lists all catalog entries visible after precedence resolution.

- default output shows `name`, supported device kinds, and one-line summary
- `--type TYPE` filters entries by supported device kind such as `uart`
- `--verbose` additionally shows the catalog source file and whether the entry overrides a lower-precedence definition

### 7.3 `sim inspect`

Shows the full metadata for one simulator entry.

The output includes at least:

- simulator name
- simulator version
- summary and description
- supported device kinds
- catalog source file
- declared parameters and defaults
- restart policy

Machine-readable contract:

- `virtrtlabctl --json sim inspect <name>` returns exactly the daemon `result` payload for `sim.inspect`
- when multiple visible versions share the same simulator name, `--version VERSION` is required and the daemon error is `ambiguous-simulator-version`
- field names must match catalog semantics directly and remain stable across `v0.2.x`
- unknown optional catalog fields may be added later but existing fields must not change type

Human-readable contract:

- labeled lines use lowercase labels
- field order remains stable within `v0.2.x` to support documentation and golden tests
- alignment whitespace is cosmetic only and not semantically relevant
- recommended golden fixtures should compare semantic field order and values, not raw padding width

### 7.4 `sim attach`

Creates or replaces the attachment definition for one device.

Arguments:

- `<device>` is the VirtRTLab device name such as `uart0`
- `<simulator>` is a catalog name such as `loopback`

Options:

- `--version VERSION` selects one explicit simulator version when multiple versions exist
- `--auto-start` sets the attachment `auto_start` flag to `true`
- `--set key=value` provides per-attachment configuration overrides; option may be repeated

Behaviour:

- validates that the device exists
- validates that the selected simulator name exists, and if `--version` is provided validates the exact `(name, version)` pair
- fails with `ambiguous-simulator-version` when multiple visible versions share the same simulator name and `--version` is omitted
- validates that the resolved simulator supports the device kind
- validates all provided config keys against declared parameters
- replaces an existing attachment only when its current state is `attached`, `stopped`, or `failed`
- attempting replacement while the current attachment is `starting`, `running`, or `stopping` fails with a state conflict
- writes the resolved attachment state into the VirtRTLab runtime state directory
- does **not** start the simulator process implicitly; use `sim start` or profile-driven startup

### 7.5 `sim detach`

Removes the attachment definition for one device.

Behaviour:

- if a simulator is currently running for the device, `sim detach` stops it first
- attachment state and generated runtime config files are removed afterwards
- detaching an already detached device returns the control-plane error `not-attached`, which the CLI maps to exit code `4`

### 7.6 `sim start`

Starts the simulator attached to one device.

Behaviour:

- requires an existing attachment
- returns the control-plane error `not-attached` when the device exists but has no attachment
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
- stopping an attachment that is already `stopped` is a no-op success
- stopping an attachment in `attached` or `failed` returns a state conflict
- stopping a device with no attachment returns the control-plane error `not-attached`, which the CLI maps to exit code `4`

### 7.8 `sim status`

Displays runtime status for one attachment or all attachments.

Without argument:

- prints one row or JSON object per attached device

With `<device>`:

- prints detailed status for the specified device only
- if the device exists but has no attachment, the control-plane error is `not-attached`, which the CLI maps to exit code `4`

Status payload includes at least:

- device name
- simulator name
- simulator version
- attachment state
- PID if a simulator process is currently alive
- `auto_start` flag
- runtime config path
- log directory path

Machine-readable contract:

- `virtrtlabctl --json sim status` returns exactly the daemon `result` payload for aggregate `sim.status`; that payload matches the aggregate state-file shape defined in section `5.6.2`
- `virtrtlabctl --json sim status <device>` returns exactly the daemon `result` payload for per-device `sim.status`; that payload matches the per-device state-file shape defined in section `5.6.1`
- human-readable output may evolve cosmetically, but the JSON schema is part of the compatibility contract

Human-readable contract:

- aggregate output uses one attachment per line with stable field order
- detailed output uses one labeled field per line with stable label names
- absence of a PID is rendered as `pid=-` in aggregate form
- recommended golden fixtures may normalize dynamic PID and timestamp values, but must keep state labels and field names exact

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

When `--json` is used, `sim logs` is not required to wrap streamed log lines into JSON in `v0.2.0`.
The initial contract reserves structured JSON output for non-streaming `sim` subcommands.

### 7.10 Exit-code expectations

The existing global CLI exit-code model remains in force.

Recommended mapping for simulator commands:

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Operational error (spawn failure, process stop timeout, catalog parse error, filesystem error) |
| 2 | Bad arguments, ambiguous simulator version selection, invalid config syntax, or parameter type validation error |
| 3 | State conflict (already running, already attached, already starting) |
| 4 | Not found or incompatible target (unknown simulator, unknown device, incompatible target, no attachment) |

Stable JSON error envelope:

```json
{"error": "state conflict: uart0 is already running", "code": 3}
```

Rules:

- the `error` string is for operators and logs
- the numeric `code` is the scripting contract
- the same numeric meaning must be preserved in human and JSON modes
- control-plane string errors such as `ambiguous-simulator-version` and `not-attached` are preserved only inside daemon responses; the CLI contract remains numeric at this layer

Human-readable error wording guidance:

- errors should start with `[error]` in default human mode when a command emits a one-line failure summary
- state-conflict wording should name both the device and current state when available
- not-found wording should identify the missing device, simulator, or attachment explicitly

### 7.11 Reference CI validation scenarios

The following scenarios define the minimum intended validation surface for the simulator lifecycle contract.

They are written as observable behaviours, not implementation tests.

| Scenario | Expected observable result |
|---|---|
| attach then status | `sim attach` succeeds and `sim status <device>` reports `attached` with stable metadata |
| start then status | `sim start` yields `running`, non-null `pid`, non-null `started_at` |
| stop then status | `sim stop` yields `stopped`, null `pid`, populated `stopped_at` |
| crash transition | killing the simulator unexpectedly yields `failed`, null `pid`, populated `last_error` or non-zero `last_exit_code` |
| `on-failure` without supervisor | a simulator with `restart_policy = on-failure` still remains `failed` after crash until an explicit command acts |
| restart after failure | `failed -> starting -> running` is observable and clears `last_error` on success |
| detach cleanup | `sim detach` removes the per-device runtime directory and later `sim status <device>` returns `not-attached` when the device still exists |
| aggregate status regeneration | deleting aggregate `state.json` and running `sim status` regenerates a coherent aggregate view |
| lost runtime dir | removing `/run/virtrtlab/simulators/` makes `sim status` return an empty set |
| invalid `--set` syntax | malformed `--set` returns exit code `2` |
| invalid `--set` type | parameter type mismatch or overflow returns exit code `2` |
| unknown top-level parameter | `sim attach --set unknown=...` returns exit code `4` or `2` only if the CLI cannot even parse the expression; preferred result is `4` for unknown target parameter |
| ambiguous simulator version | `sim inspect` or `sim attach` without `--version` on a multiply-defined simulator returns exit code `2` |
| concurrent `sim start` | one command succeeds, the other reports a coherent state conflict or already-running result |
| concurrent `sim stop` and `sim start` | no torn state file is observed; final state matches serialized completion order |
| profile partial auto-start failure | `up --config` returns non-zero, successful earlier attachments remain observable, failed one is in `failed`, later ones remain `attached` |

Recommended test partitioning:

- CLI JSON and exit-code contract tests belong in `tests/cli/`
- lifecycle/process integration tests belong in `tests/daemon/` or later simulator-specific integration suites
- race-condition tests should assert coherent file/state outcomes, not scheduler-specific ordering

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

## 9. Decision

No open questions remain in this first draft of the simulator contract.