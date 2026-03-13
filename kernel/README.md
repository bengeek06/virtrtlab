# Kernel modules (stubs)

This folder contains initial **out-of-tree** kernel module stubs for VirtRTLab.

## Build

```bash
make
```

If you need to override the kernel build directory:

```bash
make KDIR=/path/to/kernel/build/tree
```

## Load/unload

```bash
sudo insmod virtrtlab_core.ko
sudo insmod virtrtlab_uart.ko

dmesg | tail -n 50

sudo rmmod virtrtlab_uart
sudo rmmod virtrtlab_core
```

## Notes

- These are placeholders meant to validate naming/build plumbing.
- The sysfs tree and injection control plane are specified in the repo root README.

## WSL2 (Debian) notes

On WSL2 you typically do **not** have `/lib/modules/$(uname -r)/build`, because the running kernel is Microsoft's.

### Packages

```bash
sudo apt update
sudo apt install -y \
        build-essential make git \
        bc flex bison \
        libelf-dev libssl-dev \
        dwarves pkg-config
```

### Prepare the WSL kernel tree

This is required so the kernel tree contains `vmlinux` and `Module.symvers` (needed by `modpost`).

```bash
cd ~/projects
git clone https://github.com/microsoft/WSL2-Linux-Kernel.git
cd WSL2-Linux-Kernel

# Match your running kernel tag (example):
git checkout linux-msft-wsl-6.6.87.2

# Use the running kernel config as a base (recommended)
zcat /proc/config.gz > .config
make olddefconfig

# Build enough artifacts for external module builds
make -j"$(nproc)" vmlinux
```

### Build VirtRTLab modules against it

```bash
cd /home/benjamin/projects/test/kernel
make KDIR=~/projects/WSL2-Linux-Kernel
```
