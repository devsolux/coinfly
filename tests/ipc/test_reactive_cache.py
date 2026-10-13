import unittest

from coinfly.ipc.commit_dispatcher import CommitDispatcher
from coinfly.ipc.protocol import RPCError
from coinfly.ipc.reactive_cache import ReactiveCache, ResnapshotRequired


class ReactiveCacheTests(unittest.TestCase):
    def test_missing_delta_or_changed_context_forces_resnapshot(self) -> None:
        cache = ReactiveCache()
        current = cache.apply_snapshot({"sequence": 10, "context_epoch": 3, "balance": {"equity": "10"}})
        with self.assertRaises(TypeError):
            current["balance"]["equity"] = "999"
        for delta in ({"base_sequence": 9, "sequence": 11, "context_epoch": 3, "changes": {}},
                      {"base_sequence": 10, "sequence": 11, "context_epoch": 4, "changes": {}}):
            with self.assertRaises(ResnapshotRequired):
                cache.apply_delta(delta)
        with self.assertRaises(RPCError):
            cache.apply_snapshot({"sequence": 11, "context_epoch": 2})
        next_state = cache.apply_delta({"base_sequence": 10, "sequence": 11, "context_epoch": 3,
                                       "changes": {"balance": {"equity": "9"}}})
        self.assertEqual(next_state["balance"]["equity"], "9")
        self.assertEqual(current["balance"]["equity"], "10")

    def test_risk_invalidation_must_precede_cache_publication(self) -> None:
        cache = ReactiveCache()
        published = []
        def reject(_):
            raise RuntimeError("risk failed")
        dispatcher = CommitDispatcher(cache, apply_risk=reject, publish=lambda *args: published.append(args))
        with self.assertRaises(RuntimeError):
            dispatcher.committed({"sequence": 1, "context_epoch": 1})
        self.assertIsNone(cache.snapshot())
        self.assertEqual(published, [])

    def test_worker_restart_has_a_fresh_sequence_namespace(self) -> None:
        cache = ReactiveCache()
        cache.apply_snapshot({"worker_instance_id": "old-worker", "sequence": 100, "context_epoch": 3})
        restarted = cache.apply_snapshot({"worker_instance_id": "new-worker", "sequence": 0, "context_epoch": 3})
        self.assertEqual(restarted["sequence"], 0)
        with self.assertRaises(RPCError):
            cache.apply_snapshot({"worker_instance_id": "new-worker", "sequence": 0, "context_epoch": 2})
