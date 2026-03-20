# Kernel Requirements

VirtRTLab modules are out-of-tree and compile against standard kernel headers.
Most features work on any kernel ≥ 5.15 with a stock distro config.

One subsystem requires a non-default kernel option: **GPIO emulation**.

## GPIO emulation — CONFIG_GPIO_SIM

The VirtRTLab daemon loads `gpio-sim` at runtime to create virtual GPIO chips
that the AUT (Application Under Test) sees as real hardware via the GPIO
character device API.

`gpio-sim` depends on a chain of `bool` and `tristate` options that most distro
kernels do **not** enable:

| Option | Type | Role |
|---|---|---|
| `CONFIG_GPIO_SIM` | tristate | gpio-sim driver itself |
| `CONFIG_DEV_SYNC_PROBE` | tristate | synchronised probe helper |
| `CONFIG_CONFIGFS_FS` | tristate | configfs filesystem |
| `CONFIG_IRQ_SIM` | **bool** | virtual IRQ domain (built-in) |

Because `CONFIG_IRQ_SIM` is `bool`, it must be compiled into vmlinux — a kernel
recompile and reboot are required on machines whose running kernel lacks it.

### Checking your kernel

```bash
# Fast check — any of these indicates gpio-sim is ready
modinfo gpio-sim
grep "CONFIG_GPIO_SIM=m" /boot/config-$(uname -r)
grep "devm_irq_domain_create_sim_full" /proc/kallsyms
```

### Enabling on a vanilla source tree

```bash
cd /path/to/linux-$(uname -r)
scripts/config --module CONFIG_GPIO_SIM     # selects DEV_SYNC_PROBE, CONFIGFS_FS
scripts/config --enable CONFIG_IRQ_SIM      # bool — forced =y anyway by select
make olddefconfig

# Rebuild kernel + modules + .deb packages
make -j$(nproc) bindeb-pkg
sudo dpkg -i ../linux-image-*.deb ../linux-modules-*.deb
sudo reboot
```

After reboot:

```bash
sudo modprobe gpio-sim   # loads configfs + dev-sync-probe automatically
```

### Impact on VirtRTLab features

| Feature | Without gpio-sim | With gpio-sim |
|---|---|---|
| UART emulation | ✅ full | ✅ full |
| CAN emulation (v0.2) | ✅ full | ✅ full |
| GPIO emulation | ❌ disabled | ✅ full |
| GPIO AUT tests | ❌ skipped | ✅ run |

GPIO emulation is an **optional feature** — `virtrtlab_core` and `virtrtlab_uart`
load and operate normally without it.
