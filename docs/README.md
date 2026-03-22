<!-- SPDX-License-Identifier: CC-BY-4.0 -->

# VirtRTLab — Documentation

| Document | Audience | Contents |
|---|---|---|
| [../README.md](../README.md) | All users | Project overview, installation, getting started, examples |
| Current docs without `-v0.2.0` suffix | Users / operators | Stable contract for the current implementation (`v0.1.x` world) |
| Draft docs with `-v0.2.0` suffix | Contributors / reviewers | Forward-looking contract for the planned hotplug-capable `v0.2.0` architecture |
| [virtrtlabctl.md](virtrtlabctl.md) | Users | Complete CLI reference — all commands, options, output formats, exit codes |
| [v0.2.0/virtrtlabctl-v0.2.0.md](v0.2.0/virtrtlabctl-v0.2.0.md) | Contributors | Draft CLI additions for `v0.2.0` — simulator catalog, attachments, lifecycle commands |
| [daemon.md](daemon.md) | Users | `virtrtlabd` user guide — startup, signals, reconnect, logging, systemd |
| [v0.2.0/daemon-v0.2.0.md](v0.2.0/daemon-v0.2.0.md) | Contributors | Draft daemon contract for `v0.2.0` — authoritative control plane, dynamic topology, simulator supervision |
| [v0.2.0/daemon-config-v0.2.0.md](v0.2.0/daemon-config-v0.2.0.md) | Contributors | Draft daemon configuration contract for `v0.2.0` — runtime paths, socket layout, logging |
| [sysfs.md](sysfs.md) | Users / integrators | sysfs attribute reference — buses, devices, fault attrs, stats, error conditions |
| [socket-api.md](socket-api.md) | Integrators / contributors | Wire device and daemon socket transport specification, epoll loop, relay state machine |
| [v0.2.0/socket-api-v0.2.0.md](v0.2.0/socket-api-v0.2.0.md) | Integrators / contributors | Draft `v0.2.0` device dataplane socket specification — per-device sockets separate from control |
| [v0.2.0/control-socket-v0.2.0.md](v0.2.0/control-socket-v0.2.0.md) | Integrators / contributors | Draft `v0.2.0` control-plane protocol — JSONL request/response API over `/run/virtrtlab/control.sock` |
| [v0.2.0/simulator-contract-v0.2.0.md](v0.2.0/simulator-contract-v0.2.0.md) | Integrators / contributors | Draft simulator contract for `v0.2.0` — catalog format, attachment model, runtime environment |
| [v0.2.0/driver-contract-v0.2.0.md](v0.2.0/driver-contract-v0.2.0.md) | Integrators / contributors | Draft `v0.2.0` driver contract — hotplug, discovery, observability, and control obligations |
| [spec.md](spec.md) | Contributors | Architecture and interface specification — naming conventions, data paths, design decisions, milestones |
| [v0.2.0/spec-v0.2.0.md](v0.2.0/spec-v0.2.0.md) | Contributors | Draft `v0.2.0` architecture — dynamic topology, daemonized control plane, driver integration |
| [v0.2.0/privilege-model-v0.2.0.md](v0.2.0/privilege-model-v0.2.0.md) | Contributors | Draft `v0.2.0` privilege model — non-root daemon-mediated control, socket permissions, service role |
