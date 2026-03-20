# SPDX-License-Identifier: GPL-2.0-only
#
# VirtRTLab — root Makefile
#
# Delegates build to kernel/ and daemon/; owns install/uninstall/check/dkms.
#
# Targets:
#   all          — build kernel modules and daemon
#   clean        — clean all build artefacts
#   check        — verify build and install dependencies
#   install      — install on the system (requires root)
#   install-dev  — install + targeted sudoers for CI/dev (requires root)
#   uninstall    — remove all installed files (requires root)
#   dkms-add     — register source tree with DKMS (requires root)

VERSION  := 0.1.0
KDIR     ?= /lib/modules/$(shell uname -r)/build
KVER     := $(shell uname -r)

# Install paths
MODDIR   := /lib/modules/$(KVER)/extra/virtrtlab
BINDIR   := /usr/local/bin
UDEVDIR  := /lib/udev/rules.d
SYSDDIR  := /lib/systemd/system
SUDOERS  := /etc/sudoers.d
DKMSRC   := /usr/src/virtrtlab-$(VERSION)

.PHONY: all clean check install install-dev uninstall dkms-add

# ── build ─────────────────────────────────────────────────────────────────────

all:
	$(MAKE) -C kernel KDIR=$(KDIR)
	$(MAKE) -C daemon

clean:
	$(MAKE) -C kernel KDIR=$(KDIR) clean
	$(MAKE) -C daemon clean

# ── dependency check ──────────────────────────────────────────────────────────

check:
	@fails=0; warns=0; \
	echo "=== VirtRTLab $(VERSION) dependency check ==="; \
	echo ""; \
	\
	if [ -d "$(KDIR)" ]; then \
		printf "[OK]   kernel headers : %s\n" "$(KDIR)"; \
	else \
		printf "[FAIL] kernel headers not found: %s\n" "$(KDIR)"; \
		printf "       Install with: sudo apt install linux-headers-$$(uname -r)\n"; \
		fails=$$((fails + 1)); \
	fi; \
	\
	if command -v gcc >/dev/null 2>&1; then \
		printf "[OK]   gcc            : %s\n" "$$(gcc --version | head -1)"; \
	else \
		echo "[FAIL] gcc not found — install with: sudo apt install build-essential"; \
		fails=$$((fails + 1)); \
	fi; \
	\
	if python3 -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" 2>/dev/null; then \
		printf "[OK]   python         : %s\n" "$$(python3 --version)"; \
		if [ -f cli/pyproject.toml ]; then \
			deps=$$(python3 -c \
				"import tomllib; d=tomllib.load(open('cli/pyproject.toml','rb')); print(' '.join(d.get('project',{}).get('dependencies',[])))"); \
			if [ -z "$$deps" ]; then \
				echo "[OK]   cli deps       : stdlib only"; \
			else \
				for dep in $$deps; do \
					pkg=$$(echo "$$dep" | sed 's/[^a-zA-Z0-9_-].*//; s/-/_/g'); \
					if python3 -c "import $$pkg" 2>/dev/null; then \
						printf "[OK]   cli dep        : %s\n" "$$dep"; \
					else \
						printf "[WARN] cli dep missing : %s (pip install %s)\n" "$$dep" "$$dep"; \
						warns=$$((warns + 1)); \
					fi; \
				done; \
			fi; \
		fi; \
	else \
		echo "[FAIL] python >= 3.11 required"; \
		echo "       Current: $$(python3 --version 2>/dev/null || echo not found)"; \
		fails=$$((fails + 1)); \
	fi; \
	\
	dkms_bin=""; \
	if command -v dkms >/dev/null 2>&1; then \
		dkms_bin=$$(command -v dkms); \
	elif [ -x /usr/sbin/dkms ]; then \
		dkms_bin=/usr/sbin/dkms; \
	elif [ -x /sbin/dkms ]; then \
		dkms_bin=/sbin/dkms; \
	fi; \
	if [ -n "$$dkms_bin" ]; then \
		printf "[OK]   dkms           : %s\n" "$$($$dkms_bin --version | head -1)"; \
	else \
		echo "[WARN] dkms not found — make dkms-add unavailable (apt install dkms)"; \
		warns=$$((warns + 1)); \
	fi; \
	\
	if command -v udevadm >/dev/null 2>&1; then \
		echo "[OK]   udevadm"; \
	else \
		echo "[WARN] udevadm not found — make install will fail"; \
		warns=$$((warns + 1)); \
	fi; \
	\
	if command -v systemctl >/dev/null 2>&1; then \
		echo "[OK]   systemctl"; \
	else \
		echo "[WARN] systemctl not found — service unit install unavailable"; \
		warns=$$((warns + 1)); \
	fi; \
	\
	if modinfo gpio-sim >/dev/null 2>&1 || \
	   /usr/sbin/modinfo gpio-sim >/dev/null 2>&1 || \
	   grep -q "^CONFIG_GPIO_SIM=y" /boot/config-$(KVER) 2>/dev/null; then \
		echo "[OK]   gpio-sim"; \
	else \
		echo "[WARN] gpio-sim not available — GPIO emulation will be disabled at runtime"; \
		echo "       Requires CONFIG_GPIO_SIM=m in kernel (select IRQ_SIM, CONFIGFS_FS,"; \
		echo "       DEV_SYNC_PROBE). Most distro kernels do NOT ship this — kernel"; \
		echo "       recompile required. See docs/kernel-requirements.md"; \
		warns=$$((warns + 1)); \
	fi; \
	\
	echo ""; \
	if [ $$fails -gt 0 ]; then \
		echo "=== $$fails failure(s), $$warns warning(s) — fix failures before building ==="; \
		exit 1; \
	else \
		echo "=== 0 failure(s), $$warns warning(s) — build is possible ==="; \
	fi

# ── root guard ────────────────────────────────────────────────────────────────

_check_root:
	@[ "$$(id -u)" = "0" ] || { \
		echo "[FAIL] This target requires root. Use: sudo make $(MAKECMDGOALS)"; \
		exit 1; \
	}

# ── install ───────────────────────────────────────────────────────────────────

install: _check_root all
	@echo "=== Installing VirtRTLab $(VERSION) ==="
	install -d $(MODDIR)
	install -m 644 kernel/virtrtlab_core.ko \
	              kernel/virtrtlab_uart.ko \
	              kernel/virtrtlab_gpio.ko \
	              $(MODDIR)/
	depmod -a
	install -m 755 daemon/virtrtlabd   $(BINDIR)/virtrtlabd
	install -m 755 cli/virtrtlabctl.py $(BINDIR)/virtrtlabctl
	install -m 644 install/90-virtrtlab.rules $(UDEVDIR)/90-virtrtlab.rules
	udevadm control --reload-rules
	udevadm trigger
	install -m 644 install/virtrtlab.service $(SYSDDIR)/virtrtlab.service
	getent group  virtrtlab >/dev/null 2>&1 || groupadd --system virtrtlab
	getent passwd virtrtlab >/dev/null 2>&1 || \
	    useradd --system --gid virtrtlab --no-create-home \
	            --home-dir /nonexistent --shell /usr/sbin/nologin virtrtlab
	systemctl daemon-reload
	@echo ""
	@echo "=== Installation complete ==="
	@echo "    Post-install steps for each user:"
	@echo "      sudo usermod -aG virtrtlab \$$USER"
	@echo "      newgrp virtrtlab   # or log out / log in"

install-dev: install
	@echo "=== Installing development/CI sudoers fragment ==="
	install -m 440 install/virtrtlab-dev.sudoers $(SUDOERS)/virtrtlab-dev
	visudo -cf $(SUDOERS)/virtrtlab-dev
	@echo "=== install-dev complete ==="

# ── uninstall ─────────────────────────────────────────────────────────────────

uninstall: _check_root
	@echo "=== Uninstalling VirtRTLab ==="
	-systemctl stop    virtrtlab 2>/dev/null || true
	-systemctl disable virtrtlab 2>/dev/null || true
	rm -f $(SYSDDIR)/virtrtlab.service
	-systemctl daemon-reload 2>/dev/null || true
	rm -f $(UDEVDIR)/90-virtrtlab.rules
	-udevadm control --reload-rules 2>/dev/null || true
	rm -f $(BINDIR)/virtrtlabd $(BINDIR)/virtrtlabctl
	rm -f $(MODDIR)/virtrtlab_core.ko \
	      $(MODDIR)/virtrtlab_uart.ko \
	      $(MODDIR)/virtrtlab_gpio.ko
	-rmdir $(MODDIR) 2>/dev/null || true
	-depmod -a 2>/dev/null || true
	rm -f $(SUDOERS)/virtrtlab-dev
	-userdel  virtrtlab 2>/dev/null || true
	-groupdel virtrtlab 2>/dev/null || true
	@echo ""
	@echo "=== Uninstall complete ==="

# ── dkms ──────────────────────────────────────────────────────────────────────

dkms-add: _check_root
	@command -v dkms >/dev/null 2>&1 || { \
		echo "[FAIL] dkms not installed — sudo apt install dkms"; \
		exit 1; \
	}
	@echo "=== Registering virtrtlab $(VERSION) with DKMS ==="
	install -d $(DKMSRC)
	cp -a . $(DKMSRC)/
	$(MAKE) -C $(DKMSRC)/kernel KDIR=$(KDIR) clean 2>/dev/null || true
	$(MAKE) -C $(DKMSRC)/daemon clean 2>/dev/null || true
	dkms add virtrtlab/$(VERSION)
	@echo ""
	@echo "    Source registered at: $(DKMSRC)"
	@echo "    Build and install:"
	@echo "      sudo dkms build   virtrtlab/$(VERSION)"
	@echo "      sudo dkms install virtrtlab/$(VERSION)"

