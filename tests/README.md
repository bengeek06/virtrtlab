<!-- SPDX-License-Identifier: CC-BY-4.0 -->

# Test strategy

All automated validation lives under `tests/` so local runs and GitHub Actions use the same layout, fixtures, and reporting.

## Principles

- One top-level test tree for the whole project
- `pytest` is the primary runner and report producer
- Small C helper programs are allowed where POSIX or kernel-facing behaviour must be exercised precisely
- Kernel integration tests validate only **observable behaviour**: sysfs, device nodes, daemon sockets, exit codes, logs, and counters

## Recommended layout

```text
tests/
|-- README.md
|-- kernel/
|-- daemon/
|-- cli/
|-- helpers/
|-- fixtures/
`-- artifacts/
```

## Test families

### `tests/kernel/`

Scope:

- module load/unload ordering
- sysfs contract checks for `virtrtlab_core`, `virtrtlab_uart`, and `virtrtlab_gpio`
- UART observable behaviour: termios mirrors, buffering, fault injection, reset semantics
- GPIO observable behaviour: direction/value transitions, edge-related behaviour, reset semantics

Implementation guidance:

- Use pytest fixtures to provision and reset the bus before each test
- Keep shelling-out centralized in helpers so `insmod`, `rmmod`, `modprobe`, and `dmesg` collection are consistent
- Use small C binaries from `tests/helpers/` for `poll()`, blocking I/O, and other timing-sensitive kernel ABI checks

### `tests/daemon/`

Scope:

- socket creation/removal under `/run/virtrtlab/`
- single-client policy
- reconnect, drain, and cleanup rules
- backpressure/error propagation between wire device and socket endpoints
- simulator lifecycle process tests: spawn, stop, crash detection, and runtime-state coherence

Implementation guidance:

- Prefer Python sockets and subprocess fixtures
- Mock kernel endpoints only for narrow unit tests; keep relay semantics covered by real integration tests as soon as the wire device exists

### `tests/cli/`

Scope:

- argument parsing
- human-readable output
- `--json` output stability
- exit code contract
- daemon lifecycle subcommands
- simulator catalog, attachment, and lifecycle commands
- `--set` parsing and type-validation contract
- partial startup semantics for `up --config`

Implementation guidance:

- Run the CLI as a subprocess exactly as CI will
- Keep expected outputs in `tests/fixtures/`
- Prefer golden JSON fixtures for `sim list`, `sim inspect`, and `sim status`
- Prefer golden human-readable fixtures for the stable column order and field labels of `sim list`, `sim inspect`, and `sim status`
- Ensure golden fixtures cover simulator version visibility in `sim inspect`, `sim list --verbose`, and `sim status <device>`
- Race-oriented tests should assert coherent observable outcomes, not scheduler-specific ordering

Golden fixture contract for simulator CLI:

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

Comparison rules:

- compare `.json` fixtures structurally after JSON parsing
- normalize dynamic fields such as PID, instance ID, and timestamps before JSON fixture comparison
- compare `.txt` fixtures after trimming trailing spaces and allowing cosmetic alignment differences only where the spec explicitly says alignment is cosmetic
- do not normalize marker words, labels, enum values, device names, simulator names, or `auto_start=yes|no`

Recommended placeholder set used by the test harness:

- `PID`
- `INSTANCE_ID`
- `TIMESTAMP`

Recommended `v0.2.0` simulator test matrix:

- `sim attach` creates runtime state and `sim status <device>` reports `attached`
- `sim start` transitions to `running` with non-null `pid`
- `sim stop` transitions to `stopped` with null `pid`
- unexpected simulator exit transitions to `failed`
- `test-stub mode=fail` exercises deterministic startup failure
- `test-stub mode=crash` exercises deterministic post-start crash handling
- `test-stub ignore_sigterm=true` exercises stop timeout and force-kill fallback
- `restart_policy = on-failure` remains informational only in `v0.2.0`; no autonomous restart occurs
- invalid `--set` syntax returns exit code `2`
- invalid `--set` type or integer overflow returns exit code `2`
- unknown simulator or device returns exit code `4`
- concurrent lifecycle commands on one device never produce torn JSON state
- deleting `/run/virtrtlab/simulators/state.json` forces aggregate regeneration on the next status query
- deleting `/run/virtrtlab/simulators/` makes aggregate status empty and per-device status not found
- `up --config` with mixed auto-start outcomes returns non-zero and exposes partial startup clearly
- human-readable success and error prefixes stay within the `[ok]`, `[info]`, `[warn]`, `[error]` set
- aggregate status keeps field order `device`, `simulator=`, `state=`, `pid=`, `auto_start=`
- detailed status and inspect outputs expose simulator version as part of validation identity

### `tests/helpers/`

Keep helpers narrow and disposable. Expected first helpers:

- UART writer/reader exercising `termios`, `O_NONBLOCK`, and `poll()`
- GPIO consumer waiting on edge-related behaviour
- timing probe for latency/jitter assertions with explicit tolerances

Build these helpers with a local `Makefile` in `tests/helpers/` rather than introducing CMake/CTest in `v0.1.0`.

## GitHub Actions shape

Recommended split:

1. `userspace-fast`: Python linting, CLI tests, daemon unit tests, documentation checks
2. `kernel-integration`: privileged VM or self-hosted runner, module tests, UART/GPIO end-to-end scenarios
3. `artifacts`: upload `dmesg`, daemon logs, exported stats, and JUnit XML

For the simulator contract introduced in `v0.2.0`:

1. `userspace-fast` should cover catalog parsing, `--json` stability, `--set` validation, and synthetic lifecycle tests with stub simulators
2. integration jobs should cover real socket-connected simulator processes, crash handling, log capture, and partial `up --config` startup behaviour

Recommended simulator split:

1. use `test-stub` for deterministic lifecycle, log, and failure-path tests
2. use `loopback` for the simplest real socket-connected smoke path

Hosted GitHub runners are fine for userspace-only checks. Kernel module tests should run in a privileged VM or dedicated self-hosted runner so module loading, `/sys/kernel/virtrtlab/`, and device nodes behave predictably.

## Rationale

This hybrid approach keeps the harness simple:

- Python handles orchestration, assertions, fixtures, and CI reporting well
- C is used only where it materially improves fidelity against the Linux userspace ABI
- the project avoids adopting a second general-purpose build system before it has enough native userspace code to justify it