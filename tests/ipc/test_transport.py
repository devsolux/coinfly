from __future__ import annotations

import errno
import os
import stat
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path

from coinfly.ipc.auth import HelperAttestation, OwnerOnlySandboxPolicy, PeerIdentity, SignedHelperPolicy
from coinfly.ipc.protocol import FrameDecoder, RPCError, RPCRequest, decode_json, encode_frame
from coinfly.ipc.transport_named_pipe import NamedPipeRPCClient
from coinfly.ipc.transport_unix import UnixRPCClient, UnixRPCServer


def request(method: str = "state.snapshot", **changes: object) -> RPCRequest:
    data = {"jsonrpc": "2.0", "id": "request-1", "method": method, "params": {}, "epoch": 7,
            "deadline_ms": int(time.time() * 1000) + 5000}
    data.update(changes)
    return RPCRequest.parse(data)


class ProtocolTests(unittest.TestCase):
    def test_fragmented_frames_and_json_injection(self) -> None:
        expected = request().as_dict()
        frame = encode_frame(expected)
        decoder = FrameDecoder()
        decoded = []
        for value in frame:
            decoded.extend(decoder.feed(bytes([value])))
        self.assertEqual(decoded, [expected])
        self.assertFalse(decoder.pending)
        for payload in (b'{"epoch":1,"epoch":2}', b'{"value":NaN}', b'{"value":9223372036854775808}', b'[]'):
            with self.assertRaises(RPCError):
                decode_json(payload)
        with self.assertRaisesRegex(RPCError, "FRAME_LIMIT"):
            FrameDecoder().feed(struct.pack("!I", 2**32 - 1))

    def test_context_deadline_and_method_bounds(self) -> None:
        for changes in ({"epoch": True}, {"epoch": -1}, {"deadline_ms": 0},
                        {"deadline_ms": int(time.time() * 1000) + 60000}, {"method": "broker.transfer"},
                        {"id": 123}, {"params": []}):
            with self.subTest(changes=changes), self.assertRaises(RPCError):
                request(**changes)

    def test_no_unsigned_live_or_credential_authority(self) -> None:
        if os.name == "nt":
            self.skipTest("Unix UID policy; Windows named-pipe adapter is release-gated")
        peer = PeerIdentity(os.getuid(), os.getgid(), os.getpid())
        owner = OwnerOnlySandboxPolicy()
        owner.authorize(peer, request())
        with self.assertRaisesRegex(RPCError, "SIGNED_HELPER_REQUIRED"):
            owner.authorize(peer, request("engine.arm"))
        with self.assertRaisesRegex(RPCError, "PEER_OWNER_MISMATCH"):
            owner.authorize(PeerIdentity(peer.uid + 1, peer.gid), request())
        sandbox = OwnerOnlySandboxPolicy(realm_resolver=lambda _: "SANDBOX")
        sandbox.authorize(peer, request("context.apply"))
        with self.assertRaisesRegex(RPCError, "SIGNED_HELPER_REQUIRED"):
            sandbox.authorize(peer, request("credentials.enroll"))
        for requirement, identifier, pid in (("wrong", "com.devsolux.coinfly", peer.pid),
                                             ("pinned", "unsigned-app", peer.pid),
                                             ("pinned", "com.devsolux.coinfly", peer.pid + 1)):
            policy = SignedHelperPolicy(verifier=lambda _: HelperAttestation(True, identifier, pid, requirement),
                                        designated_requirement="pinned")
            with self.assertRaises(RPCError):
                policy.authorize(peer, request("engine.arm"))

    @unittest.skipIf(os.name == "nt", "off-platform guard test")
    def test_named_pipe_cannot_silently_fall_back_off_windows(self) -> None:
        with self.assertRaisesRegex(RPCError, "WINDOWS_IPC_REQUIRES_WINDOWS"):
            NamedPipeRPCClient("ignored").connect()

    def test_signed_client_requires_exact_application_identifier(self) -> None:
        peer = PeerIdentity(uid=123, gid=123, pid=456)
        for identifier, allowed in (("com.devsolux.coinfly", True),
                                    ("com.devsolux.coinfly.worker", False),
                                    ("com.devsolux.coinfly.other", False),
                                    ("com.devsolux.coinfly-malicious", False)):
            policy = SignedHelperPolicy(
                verifier=lambda _: HelperAttestation(True, identifier, peer.pid, "pinned"),
                designated_requirement="pinned", owner_uid=peer.uid,
            )
            with self.subTest(identifier=identifier):
                if allowed:
                    policy.authorize(peer, request("credentials.enroll"))
                else:
                    with self.assertRaisesRegex(RPCError, "SIGNED_HELPER_REQUIRED"):
                        policy.authorize(peer, request("credentials.enroll"))


@unittest.skipIf(os.name == "nt", "Unix peer-auth tests require a Unix host")
class UnixRoundTripTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="cf-ipc-")
        os.chmod(self.directory.name, 0o700)
        self.path = Path(self.directory.name) / "worker.sock"

    def tearDown(self) -> None:
        self.directory.cleanup()

    def start_server(self, server: UnixRPCServer) -> UnixRPCServer:
        try:
            return server.start()
        except PermissionError as exc:
            if exc.errno == errno.EPERM:
                self.skipTest("Host sandbox denies AF_UNIX socket creation; real native IPC qualification is outstanding")
            raise

    def test_roundtrip_peer_permissions_and_redacted_errors(self) -> None:
        peers: list[PeerIdentity] = []
        def handle(value: RPCRequest, peer: PeerIdentity) -> object:
            peers.append(peer)
            if value.method == "context.preview":
                raise RuntimeError("secret-canary-do-not-leak")
            return {"sequence": 12, "context_epoch": value.epoch}
        server = self.start_server(UnixRPCServer(self.path, handle))
        try:
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
            with UnixRPCClient(self.path) as client:
                self.assertEqual(client.call("state.snapshot", epoch=3), {"sequence": 12, "context_epoch": 3})
                with self.assertLogs("coinfly.ipc.transport_unix", "ERROR") as logged:
                    with self.assertRaisesRegex(RPCError, "INTERNAL_ERROR") as error:
                        client.call("context.preview", epoch=3)
                self.assertNotIn("secret-canary", str(error.exception))
                # The defect is traceable (method and type) without logging the message.
                self.assertIn("method=context.preview error=RuntimeError", "\n".join(logged.output))
                self.assertNotIn("secret-canary", "\n".join(logged.output))
            self.assertEqual(peers[0].uid, os.getuid())
            with UnixRPCClient(self.path) as client:
                with self.assertRaisesRegex(RPCError, "SIGNED_HELPER_REQUIRED"):
                    client.call("engine.arm", epoch=3)
        finally:
            server.close()
        self.assertFalse(self.path.exists())

    def test_subscribe_commit_during_snapshot_is_never_missed(self) -> None:
        server: UnixRPCServer
        def handle(value: RPCRequest, _: PeerIdentity) -> object:
            if value.method == "state.subscribe":
                server.publish_notification("state.snapshot", {"sequence": 11, "context_epoch": 1})
                return {"sequence": 10, "context_epoch": 1}
            return {}
        server = UnixRPCServer(self.path, handle)
        self.start_server(server)
        try:
            with UnixRPCClient(self.path) as client:
                self.assertEqual(client.call("state.subscribe")["sequence"], 10)
                message = next(client.notifications(threading.Event()))
                self.assertEqual(message["params"]["sequence"], 11)
        finally:
            server.close()

    def test_insecure_directory_and_existing_file_rejected(self) -> None:
        os.chmod(self.directory.name, 0o755)
        with self.assertRaisesRegex(RPCError, "IPC_DIRECTORY_NOT_PRIVATE"):
            UnixRPCServer(self.path, lambda *_: {}).start()
        os.chmod(self.directory.name, 0o700)
        self.path.write_text("must remain untouched")
        with self.assertRaisesRegex(RPCError, "IPC_ENDPOINT_EXISTS"):
            UnixRPCServer(self.path, lambda *_: {}, cleanup_stale=True).start()
        self.assertEqual(self.path.read_text(), "must remain untouched")


if __name__ == "__main__":
    unittest.main()
