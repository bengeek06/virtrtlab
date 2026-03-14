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

Implementation guidance:

- Run the CLI as a subprocess exactly as CI will
- Keep expected outputs in `tests/fixtures/`

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

Hosted GitHub runners are fine for userspace-only checks. Kernel module tests should run in a privileged VM or dedicated self-hosted runner so module loading, `/sys/kernel/virtrtlab/`, and device nodes behave predictably.

## Rationale

This hybrid approach keeps the harness simple:

- Python handles orchestration, assertions, fixtures, and CI reporting well
- C is used only where it materially improves fidelity against the Linux userspace ABI
- the project avoids adopting a second general-purpose build system before it has enough native userspace code to justify it