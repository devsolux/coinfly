"""OS peer ownership is independent from signed helper attestation."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from coinfly import __application_id__

from .protocol import OWNER_METHODS, READ_METHODS, RPCError, RPCRequest


def current_uid() -> int:
    reader = getattr(os, "getuid", None)
    if reader is None:
        from .windows_native import principal_id, process_sid
        return principal_id(process_sid(os.getpid()))
    return int(reader())


@dataclass(frozen=True, slots=True)
class PeerIdentity:
    uid: int
    gid: int
    pid: int | None = None
    # Assigned by the transport per accepted connection; the handshake binds
    # protocol and build identity to it (pids can be reused, ids cannot).
    connection_id: int | None = None


class AuthorizationPolicy(Protocol):
    def authorize(self, peer: PeerIdentity, request: RPCRequest) -> None: ...


class OwnerOnlySandboxPolicy:
    """Read-only by default. Sandbox mutation needs a trusted worker realm resolver.

    The resolver must inspect durable effective AND proposed contexts. Reading
    the requested environment alone would permit a LIVE→SANDBOX drain bypass.
    UID equality does not prove that a client is a signed Coinfly helper.
    """

    def __init__(self, *, owner_uid: int | None = None,
                 realm_resolver: Callable[[RPCRequest], str] | None = None) -> None:
        self.owner_uid = current_uid() if owner_uid is None else owner_uid
        self.realm_resolver = realm_resolver

    def authorize(self, peer: PeerIdentity, request: RPCRequest) -> None:
        if peer.uid != self.owner_uid:
            raise RPCError("PEER_OWNER_MISMATCH")
        if request.method in READ_METHODS or request.method in OWNER_METHODS:
            return
        if request.method == "credentials.enroll":
            raise RPCError("SIGNED_HELPER_REQUIRED")
        if self.realm_resolver is not None and self.realm_resolver(request) == "SANDBOX":
            return
        raise RPCError("SIGNED_HELPER_REQUIRED")


@dataclass(frozen=True, slots=True)
class HelperAttestation:
    """Construct only from the native code-signing verifier, never an RPC payload."""

    verified: bool
    application_identifier: str
    process_id: int
    designated_requirement: str


class SignedHelperPolicy(OwnerOnlySandboxPolicy):
    def __init__(self, *, verifier: Callable[[PeerIdentity], HelperAttestation],
                 designated_requirement: str, owner_uid: int | None = None,
                 realm_resolver: Callable[[RPCRequest], str] | None = None) -> None:
        super().__init__(owner_uid=owner_uid, realm_resolver=realm_resolver)
        if not designated_requirement:
            raise ValueError("A pinned native signing requirement is required")
        self.verifier = verifier
        self.designated_requirement = designated_requirement

    def authorize(self, peer: PeerIdentity, request: RPCRequest) -> None:
        try:
            super().authorize(peer, request)
            return
        except RPCError as exc:
            if exc.code != "SIGNED_HELPER_REQUIRED":
                raise
        if peer.pid is None:
            raise RPCError("SIGNED_HELPER_UNVERIFIABLE")
        proof = self.verifier(peer)
        if not (proof.verified and proof.application_identifier == __application_id__
                and proof.process_id == peer.pid
                and proof.designated_requirement == self.designated_requirement):
            raise RPCError("SIGNED_HELPER_REQUIRED")
