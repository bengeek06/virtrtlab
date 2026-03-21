<!-- SPDX-License-Identifier: CC-BY-4.0 -->

# Kernel Requirements

VirtRTLab modules are out-of-tree and compile against standard kernel headers.
The full list of required `CONFIG_` options is below.

## Standard options (present in all Debian/Ubuntu kernels)

These are `tristate` or `bool` options already enabled in every stock
Debian/Ubuntu kernel ≥ 5.15. No kernel recompilation is needed for these.

| Option | Type | Required by |
|---|---|---|
| `CONFIG_TTY` | bool | `virtrtlab_uart` TTY driver |
| `CONFIG_GPIOLIB` | bool | `virtrtlab_gpio` gpio_chip registration |
| `CONFIG_GPIO_CDEV` | bool | `/dev/gpiochipN` character device API |
| `CONFIG_GPIO_SYSFS` | bool | legacy `/sys/class/gpio` ABI (optional; absent → `sysfs_base` attr not exposed) |

Quick check:

```bash
grep -E "CONFIG_TTY|CONFIG_GPIOLIB|CONFIG_GPIO_CDEV" /boot/config-$(uname -r)
```

All must be `=y`.

## CONFIG_IRQ_SIM — planned for v0.2.0 (GPIO edge events)

> **Status (v0.1.0)**: `virtrtlab_gpio` does not yet implement the irqchip /
> `irq_sim` integration. GPIO edge-event notifications to the AUT via the
> GPIO v2 character device API are **planned for v0.2.0**. The current driver
> only exposes the `inject` sysfs attribute for harness-driven line transitions.
> `CONFIG_IRQ_SIM` is **not required** to build or run `virtrtlab_gpio` today.

When the irqchip path is implemented, `virtrtlab_gpio` will use the **simulated
IRQ domain** infrastructure (`irq_sim`) to back edge-event notifications. This
infrastructure is guarded by:

```
CONFIG_IRQ_SIM   (bool)
```

Because it is a `bool` (not `tristate`), it **cannot be built as a module**: it
must be compiled into `vmlinux`. If the running kernel lacks it, a kernel rebuild
and reboot will be required before `virtrtlab_gpio` can deliver edge events to the
AUT.

### Checking your kernel (for future use)

```bash
grep CONFIG_IRQ_SIM /boot/config-$(uname -r)
# Expected: CONFIG_IRQ_SIM=y
# Bad:      # CONFIG_IRQ_SIM is not set
```

### Impact (v0.2.0 and later)

| Feature | Without `CONFIG_IRQ_SIM` | With `CONFIG_IRQ_SIM=y` |
|---|---|---|
| UART emulation | ✅ full | ✅ full |
| GPIO line state injection | ✅ full | ✅ full |
| GPIO edge events to AUT | ❌ not available (v0.2.0+) | ✅ full (v0.2.0+) |
| GPIO AUT tests (edge-based) | ❌ skipped by pytest | ✅ run |

GPIO **state injection** (`virtrtlabctl inject`) and UART emulation work without
`CONFIG_IRQ_SIM`. Only AUTs that listen for GPIO edge events via
`GPIO_V2_LINE_EVENT_*` will be affected (once that feature is implemented).

### Enabling CONFIG_IRQ_SIM — Debian/Ubuntu procedure

See [../README.md#recompiling-the-kernel](../README.md#recompiling-the-kernel-on-debianubuntu-advanced)
for the full step-by-step. The only option to add is:

```bash
scripts/config --enable CONFIG_IRQ_SIM
make olddefconfig
```

`CONFIG_IRQ_SIM` has no sub-dependencies. It does not pull in `gpio-sim`,
`configfs`, or any other subsystem.

## What VirtRTLab does NOT use

The following kernel features are **not** required and were considered but
rejected in the current architecture:

| Rejected option | Reason |
|---|---|
| `CONFIG_GPIO_SIM` | VirtRTLab registers its own `gpio_chip` directly via `gpiochip_add_data()` — no dependency on the in-tree `gpio-sim` driver |
| `CONFIG_CONFIGFS_FS` | configfs is the runtime configuration interface for `gpio-sim`; not used by `virtrtlab_gpio` |
| `CONFIG_DEV_SYNC_PROBE` | synchronised probe helper required by `gpio-sim`; not needed by `virtrtlab_gpio` |
