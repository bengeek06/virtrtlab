<!-- SPDX-License-Identifier: CC-BY-4.0 -->

# CLI tooling

The `v0.1.0` CLI target is split into two roles:

- `virtrtlabd` — daemon for streamed peripherals in the MVP (UART first), exposing one AF_UNIX socket per device under `/run/virtrtlab/`
- `virtrtlabctl` — CLI for discovery, sysfs get/set helpers, stats export, and daemon lifecycle

## Current repository status

The repository currently contains an early `virtrtlabctl.py` prototype. The canonical contract is defined in the root [README.md](../README.md), and the intended test layout is described in [../tests/README.md](../tests/README.md).

## v0.1.0 direction

- keep the CLI focused on sysfs and process orchestration
- keep the daemon focused on raw socket relay for UART-class devices
- validate both through the centralized `tests/` tree rather than ad-hoc scripts
