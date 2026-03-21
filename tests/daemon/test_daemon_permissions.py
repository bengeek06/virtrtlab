# SPDX-License-Identifier: MIT

"""
test_daemon_permissions.py — socket permission tests for virtrtlabd (issue #47).

Verifies that the daemon creates the AF_UNIX socket with the correct
permissions so that non-root users in the virtrtlab group can connect
without CLI intervention.

Acceptance criteria covered:
  - socket mode is srw-rw---- (0660), not srwxr-xr-x (0755)
  - socket group is 'virtrtlab'
  - world bits are clear (no o+rwx)
  - non-root virtrtlab group member can connect (skip if running as root)
"""

import grp
import os
import socket
import stat

import pytest

from conftest import DAEMON_BIN, RUN_DIR, SOCK_PATH


VIRTRTLAB_GROUP = "virtrtlab"


def _virtrtlab_gid():
    """Return the GID of 'virtrtlab', or None if the group does not exist."""
    try:
        return grp.getgrnam(VIRTRTLAB_GROUP).gr_gid
    except KeyError:
        return None


# ---------------------------------------------------------------------------
# Socket permission tests
# ---------------------------------------------------------------------------

class TestDaemonSocketPermissions:
    """virtrtlabd creates AF_UNIX sockets with group-accessible permissions."""

    def test_socket_mode_is_0660(self, daemon_proc):
        """Socket is created with mode srw-rw---- (0o660 low bits)."""
        mode = os.stat(SOCK_PATH).st_mode & 0o777
        assert mode == 0o660, (
            f"{SOCK_PATH}: expected mode 0660, got {oct(mode)}"
        )

    def test_socket_owner_is_root(self, daemon_proc):
        """Socket is owned by root (uid 0)."""
        uid = os.stat(SOCK_PATH).st_uid
        assert uid == 0, f"{SOCK_PATH}: expected uid 0 (root), got {uid}"

    def test_socket_group_is_virtrtlab(self, daemon_proc):
        """Socket group ownership is 'virtrtlab'."""
        gid = _virtrtlab_gid()
        if gid is None:
            pytest.skip("group 'virtrtlab' does not exist on this system")
        sock_gid = os.stat(SOCK_PATH).st_gid
        assert sock_gid == gid, (
            f"{SOCK_PATH}: expected gid {gid} (virtrtlab), got {sock_gid}"
        )

    def test_socket_no_world_bits(self, daemon_proc):
        """Socket has no world-accessible permission bits (o=---)."""
        mode = os.stat(SOCK_PATH).st_mode & 0o007
        assert mode == 0, (
            f"{SOCK_PATH}: world bits should be 0, got {oct(mode)}"
        )

    def test_socket_is_unix_socket(self, daemon_proc):
        """Sanity check: the path is still an AF_UNIX socket."""
        mode = os.stat(SOCK_PATH).st_mode
        assert stat.S_ISSOCK(mode), (
            f"{SOCK_PATH}: not a socket (mode={oct(mode)})"
        )

    def test_non_root_virtrtlab_member_can_connect(self, daemon_proc):
        """
        A non-root process whose effective GID is virtrtlab can connect to the
        socket.  This test is skipped when running as root (trivially passes)
        or when the virtrtlab group does not exist.
        """
        if os.getuid() == 0:
            pytest.skip("run as non-root virtrtlab group member to exercise this test")

        gid = _virtrtlab_gid()
        if gid is None:
            pytest.skip("group 'virtrtlab' does not exist on this system")

        # Verify the current user is actually in the group before trying.
        if gid not in os.getgroups():
            pytest.skip(
                "current user is not a member of 'virtrtlab' — "
                "run: sudo usermod -aG virtrtlab $USER && newgrp virtrtlab"
            )

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(2)
            try:
                s.connect(SOCK_PATH)
            except PermissionError as exc:
                pytest.fail(
                    f"virtrtlab group member cannot connect to {SOCK_PATH}: {exc}"
                )
