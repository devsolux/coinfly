"""Shared test helpers that hold on every supported platform."""

import os

from coinfly.ipc.auth import PeerIdentity, current_uid


def local_peer() -> PeerIdentity:
    """This process as an IPC peer, as the platform transport identifies it.

    POSIX transports report uid/gid; the Windows named-pipe transport reports
    the user's principal id for both.
    """
    uid = current_uid()
    return PeerIdentity(uid=uid, gid=os.getgid() if hasattr(os, "getgid") else uid, pid=os.getpid())
