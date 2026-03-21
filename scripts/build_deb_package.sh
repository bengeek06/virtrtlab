#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 <tag-or-version> <output-dir>"
  exit 1
fi

RAW_VERSION="$1"
OUT_DIR="$2"
VERSION="${RAW_VERSION#v}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

ARCH="$(dpkg --print-architecture 2>/dev/null || echo amd64)"
PKG_NAME="virtrtlab"
PKG_FILE="${PKG_NAME}_${VERSION}_${ARCH}.deb"

STAGE_DIR="$(mktemp -d)"
trap 'rm -rf "$STAGE_DIR"' EXIT

mkdir -p "$OUT_DIR"

# Build daemon binary to include it in the package.
make -C "$REPO_ROOT/daemon"

# Package payload layout.
install -d "$STAGE_DIR/usr/src/virtrtlab-${VERSION}"
install -d "$STAGE_DIR/usr/local/bin"
install -d "$STAGE_DIR/lib/udev/rules.d"
install -d "$STAGE_DIR/lib/systemd/system"
install -d "$STAGE_DIR/etc/modules-load.d"
install -d "$STAGE_DIR/DEBIAN"

cp -a "$REPO_ROOT/kernel" "$STAGE_DIR/usr/src/virtrtlab-${VERSION}/"
cp -a "$REPO_ROOT/dkms.conf" "$STAGE_DIR/usr/src/virtrtlab-${VERSION}/"
cp -a "$REPO_ROOT/Makefile" "$STAGE_DIR/usr/src/virtrtlab-${VERSION}/"
cp -a "$REPO_ROOT/cli" "$STAGE_DIR/usr/src/virtrtlab-${VERSION}/"
cp -a "$REPO_ROOT/daemon" "$STAGE_DIR/usr/src/virtrtlab-${VERSION}/"
cp -a "$REPO_ROOT/LICENSE" "$STAGE_DIR/usr/src/virtrtlab-${VERSION}/" 2>/dev/null || true
cp -a "$REPO_ROOT/LICENSE.MIT" "$STAGE_DIR/usr/src/virtrtlab-${VERSION}/" 2>/dev/null || true
cp -a "$REPO_ROOT/LICENSE.CC-BY-4.0" "$STAGE_DIR/usr/src/virtrtlab-${VERSION}/" 2>/dev/null || true

install -m 755 "$REPO_ROOT/daemon/virtrtlabd" "$STAGE_DIR/usr/local/bin/virtrtlabd"
install -m 755 "$REPO_ROOT/cli/virtrtlabctl.py" "$STAGE_DIR/usr/local/bin/virtrtlabctl"
install -m 644 "$REPO_ROOT/install/90-virtrtlab.rules" "$STAGE_DIR/lib/udev/rules.d/90-virtrtlab.rules"
install -m 644 "$REPO_ROOT/install/virtrtlab.service" "$STAGE_DIR/lib/systemd/system/virtrtlab.service"
printf '%s\n' virtrtlab_core virtrtlab_uart virtrtlab_gpio > "$STAGE_DIR/etc/modules-load.d/virtrtlab.conf"

cat > "$STAGE_DIR/DEBIAN/control" <<EOF
Package: ${PKG_NAME}
Version: ${VERSION}
Section: kernel
Priority: optional
Architecture: ${ARCH}
Maintainer: VirtRTLab <noreply@example.com>
Depends: dkms (>= 2.8.0), kmod, udev, adduser, systemd | systemd-sysv
Description: VirtRTLab virtual peripherals (DKMS modules + daemon + CLI)
 VirtRTLab provides virtual UART/GPIO peripherals with fault injection,
 a relay daemon, and a userspace control CLI.
EOF

cat > "$STAGE_DIR/DEBIAN/postinst" <<EOF
#!/bin/sh
set -e

if ! getent group virtrtlab >/dev/null 2>&1; then
  groupadd --system virtrtlab || true
fi
if ! getent passwd virtrtlab >/dev/null 2>&1; then
  useradd --system --gid virtrtlab --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin virtrtlab || true
fi

DKMS_BIN=""
if command -v dkms >/dev/null 2>&1; then
  DKMS_BIN="\$(command -v dkms)"
elif [ -x /usr/sbin/dkms ]; then
  DKMS_BIN=/usr/sbin/dkms
elif [ -x /sbin/dkms ]; then
  DKMS_BIN=/sbin/dkms
fi

if [ -n "\$DKMS_BIN" ]; then
  \$DKMS_BIN add -m virtrtlab -v ${VERSION} || true
  \$DKMS_BIN build -m virtrtlab -v ${VERSION} || true
  \$DKMS_BIN install -m virtrtlab -v ${VERSION} || true
fi

depmod -a || true
udevadm control --reload-rules || true
udevadm trigger || true
systemctl daemon-reload || true

exit 0
EOF

cat > "$STAGE_DIR/DEBIAN/prerm" <<'EOF'
#!/bin/sh
set -e

systemctl stop virtrtlab 2>/dev/null || true
systemctl disable virtrtlab 2>/dev/null || true

exit 0
EOF

cat > "$STAGE_DIR/DEBIAN/postrm" <<EOF
#!/bin/sh
set -e

if [ "\$1" = "purge" ]; then
  DKMS_BIN=""
  if command -v dkms >/dev/null 2>&1; then
    DKMS_BIN="\$(command -v dkms)"
  elif [ -x /usr/sbin/dkms ]; then
    DKMS_BIN=/usr/sbin/dkms
  elif [ -x /sbin/dkms ]; then
    DKMS_BIN=/sbin/dkms
  fi

  if [ -n "\$DKMS_BIN" ]; then
    \$DKMS_BIN remove -m virtrtlab -v ${VERSION} --all || true
  fi
fi

exit 0
EOF

chmod 0755 "$STAGE_DIR/DEBIAN/postinst" "$STAGE_DIR/DEBIAN/prerm" "$STAGE_DIR/DEBIAN/postrm"

dpkg-deb --build "$STAGE_DIR" "$OUT_DIR/$PKG_FILE" >/dev/null

echo "Built package: $OUT_DIR/$PKG_FILE"
