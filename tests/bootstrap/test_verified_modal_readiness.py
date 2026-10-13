from __future__ import annotations

import asyncio
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from coinfly.bootstrap.manager import BootstrapManager, BootstrapState
from coinfly.bootstrap.manifest import BootstrapError
from tests.bootstrap.test_signed_manifests import Ed25519PrivateKey, fixture_root


class BootstrapResponsivenessTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_publisher_root_blocks_without_creating_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manager = BootstrapManager(Path(temporary))
            updates = manager.subscribe()
            progress = await manager.check()
            self.assertEqual(progress.state, BootstrapState.BLOCKED)
            self.assertEqual(progress.reason, "PUBLISHER_TRUST_ROOT_MISSING")
            self.assertFalse(progress.ready)
            self.assertFalse((Path(temporary) / ".devsolux").exists())
            with self.assertRaisesRegex(BootstrapError, "DATASET_NOT_READY"):
                manager.verified_dataset()
            self.assertEqual(updates.qsize(), 1)
            manager.unsubscribe(updates)

    async def test_signed_manifest_missing_is_a_separate_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manager = BootstrapManager(Path(temporary), fixture_root(Ed25519PrivateKey.generate()))
            progress = await manager.check()
            self.assertEqual(progress.reason, "SIGNED_MANIFEST_MISSING")

    async def test_transfer_complete_never_means_ready_and_safety_loop_remains_live(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manager = BootstrapManager(Path(temporary), fixture_root(Ed25519PrivateKey.generate()))
            ticks = 0

            def slow_check(*args, **kwargs):
                kwargs["progress"]("DOWNLOADING", "edges.feather", 100, 100)
                time.sleep(0.06)
                raise BootstrapError("GRAPH_ARRAY_DIGEST_MISMATCH")

            async def safety_actor() -> None:
                nonlocal ticks
                for _ in range(10):
                    await asyncio.sleep(0.005)
                    ticks += 1

            with patch("coinfly.bootstrap.manager.DatasetInstaller.check", side_effect=slow_check):
                result, _ = await asyncio.gather(manager.check(), safety_actor())
            self.assertEqual(ticks, 10)
            self.assertEqual(result.state, BootstrapState.BLOCKED)
            self.assertEqual(result.reason, "GRAPH_ARRAY_DIGEST_MISMATCH")
            self.assertFalse(result.ready)

    async def test_cancel_interrupts_paused_bootstrap_independently(self) -> None:
        started = threading.Event()

        def waiting_check(*args, **kwargs):
            controls = kwargs["controls"]
            controls.unpaused.clear()
            started.set()
            controls.checkpoint()
            raise AssertionError("cancelled setup must not resume")

        with tempfile.TemporaryDirectory() as temporary:
            manager = BootstrapManager(Path(temporary), fixture_root(Ed25519PrivateKey.generate()))
            with patch("coinfly.bootstrap.manager.DatasetInstaller.check", side_effect=waiting_check):
                operation = asyncio.create_task(manager.check())
                for _ in range(100):
                    if started.is_set():
                        break
                    await asyncio.sleep(0.001)
                manager.cancel()
                result = await asyncio.wait_for(operation, 1)
            self.assertEqual(result.state, BootstrapState.CANCELLED)

    async def test_retry_resumes_paused_install_without_a_second_installer(self) -> None:
        started, proceed = threading.Event(), threading.Event()

        def waiting_install(*args, **kwargs):
            started.set()
            if not proceed.wait(2):
                raise AssertionError("test did not release the installer")
            kwargs["controls"].checkpoint()
            raise BootstrapError("FIXTURE_FINISHED")

        with tempfile.TemporaryDirectory() as temporary:
            manager = BootstrapManager(Path(temporary), fixture_root(Ed25519PrivateKey.generate()))
            with patch("coinfly.bootstrap.manager.DatasetInstaller.install", side_effect=waiting_install) as install:
                operation = asyncio.create_task(manager.install())
                try:
                    for _ in range(100):
                        if started.is_set():
                            break
                        await asyncio.sleep(0.001)
                    self.assertTrue(started.is_set())
                    manager.pause()
                    manager._advance("DOWNLOADING", "edges.feather", 10, 100)
                    self.assertEqual(manager.progress.state, BootstrapState.PAUSED)
                    proceed.set()
                    result = await manager.retry()
                    self.assertEqual(result.state, BootstrapState.DOWNLOADING)
                    self.assertEqual(result.bytes_completed, 10)
                    self.assertFalse(result.ready)
                    result = await asyncio.wait_for(operation, 2)
                    self.assertEqual(result.reason, "FIXTURE_FINISHED")
                    self.assertEqual(install.call_count, 1)
                finally:
                    proceed.set()
                    manager.cancel()
                    await asyncio.gather(operation, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
