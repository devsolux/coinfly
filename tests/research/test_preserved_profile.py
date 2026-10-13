from __future__ import annotations

import hashlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd

from coinfly.bootstrap.manifest import MANIFESTS, BootstrapError
from coinfly.domain.context import ProductTrack
from coinfly.research.assets import verify_native_kernel
from coinfly.research.checkpoint import validate_checkpoint_archive, validate_native_state
from coinfly.research.neural.connectome import exact_ids, index_edges, normalize_nodes
from coinfly.research.neural.controller import Decoder
from coinfly.research.neural.rule import advance
from coinfly.research.neural.sensory import retinal_samples
from coinfly.research.neural.transmitters import transmitter_signs
from coinfly.research.neural_adapter import FixedDecoderWeightAdapter
from coinfly.research.profile import NeuralProfile
from coinfly.strategy.contracts import InstrumentWeight


class PreservedNeuralTests(unittest.TestCase):
    def test_original_manifest_bytes_and_native_source_are_unchanged(self) -> None:
        expected = {
            "datasets.json": "4532ef8b2f08d819cb31dee3a40f21685112971a05697cf0fa7fa634cf9035b8",
            "sources.lock.json": "a518a766aa46b14b7b9230a108a63b902acc1e3386f123f45d5c52311342efd8",
            "arrays.lock.json": "39de6a2e46be552b734b822c8452e07ed05b5e8af906c5f79db7d63c2d68515e",
            "neurons.lock.json": "89ab067db991c674f1fbe4fcaf2d8d5dabab9337e8cb6a24270ad8e8b5c582c7",
        }
        for filename, digest in expected.items():
            self.assertEqual(hashlib.sha256((MANIFESTS / filename).read_bytes()).hexdigest(), digest)
        self.assertEqual(hashlib.sha256((MANIFESTS.parent / "neural/kernel.cpp").read_bytes()).hexdigest(),
                         "e2d4d584f4633613277bb1bf6ea7ba20855c01a332ae2e9344ed7782604c8509")

    def test_integer_ids_weak_edges_and_self_edges_remain(self) -> None:
        ids = exact_ids(["9007199254740993", "9007199254740995"])
        self.assertEqual(int(ids[0]), 9007199254740993)
        pre, post, weight, keep = index_edges(ids, ids, ids[::-1], np.array([1, 2]))
        self.assertEqual(weight.tolist(), [1, 2])
        self.assertTrue(keep.all())
        _, self_post, self_weight, _ = index_edges(ids, ids, ids, np.array([1, 1]))
        self.assertEqual(self_post.tolist(), [0, 1])
        self.assertEqual(self_weight.tolist(), [1, 1])
        with self.assertRaises(ValueError):
            exact_ids([float(9007199254740993)])

    def test_node_policy_keeps_uncertain_superclass_and_excludes_explicit_glia(self) -> None:
        frame = pd.DataFrame({"bodyId": [1, 2, 3, 4], "superclass": ["tbc", "central", None, "central"],
                              "statusLabel": [None] * 4, "status": ["Untraced", "Traced", None, "Glia"],
                              "type": [None] * 4})
        catalog, nodes = normalize_nodes("malecns_v1", frame)
        self.assertEqual(nodes.source_id.tolist(), [1, 2])
        self.assertEqual(len(catalog), 4)

    def test_fixed_decoder_has_spike_gate_and_population_mean_semantics(self) -> None:
        annotations = pd.DataFrame({"type": ["DNp20", "DNp20", "DNpe017"], "somaSide": ["L", "R", "L"]})
        decoder = Decoder(np.array([1, 2, 3]), annotations, 2)
        for counts, expected in [([0, 10, 0], "HOLD"), ([0, 10, 1], "BUY"),
                                 ([10, 0, 1], "SELL"), ([4, 4, 1], "HOLD")]:
            self.assertEqual(decoder.decode(np.array(counts), .5)["side"], expected)
        with self.assertRaises(ValueError):
            decoder.decode(np.array([1, -1, 0]), .5)

    def test_temporal_rule_and_frozen_control_are_preserved(self) -> None:
        def trial(order, frozen=False):
            k, d, u, w = np.zeros(2), np.zeros(1), np.zeros(2), np.zeros(2)
            for phase in order:
                for _ in range(20):
                    kc = np.array([20., 0.]) if phase == "cue" else np.zeros(2)
                    dan = np.array([30.]) if phase == "reinforce" else np.zeros(1)
                    advance(k, d, u, w, kc, dan, np.ones((1, 2)), .01, .001, frozen=frozen)
            return w
        self.assertLess(trial(["cue", "reinforce"])[0], 0)
        self.assertGreater(trial(["reinforce", "cue"])[0], 0)
        np.testing.assert_array_equal(trial(["cue", "reinforce"], True), np.zeros(2))

    def test_transmitters_and_rgb_are_declared_sensory_inputs(self) -> None:
        signs, uncertain = transmitter_signs(["acetylcholine", "gaba", "dopamine", "acetylcholine,gaba", None])
        self.assertEqual(signs.tolist(), [1, -1, 1, 1, 1])
        self.assertEqual(uncertain.tolist(), [False, False, True, True, True])
        samples = retinal_samples(np.full((2, 2, 3), 255, dtype=np.uint8), np.array([[0., 1.]]))
        self.assertAlmostEqual(float(samples[0]), 1.)
        with self.assertRaises(ValueError):
            retinal_samples(np.zeros((2, 2, 3), dtype=np.uint8), np.array([[np.nan, 0.]]))

    def test_no_native_loader_can_invent_certification(self) -> None:
        with self.assertRaisesRegex(BootstrapError, "NATIVE_KERNEL_UNCERTIFIED"):
            verify_native_kernel(Path("unprovided.dylib"))
        self.assertEqual(NeuralProfile().profile_id, "coinfly-dual-compartment-v1")


class NeuralWeightMappingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = FixedDecoderWeightAdapter(("BTC-USD", "ETH-USD"))
        self.previous = (InstrumentWeight(instrument_id="BTC-USD", weight=Decimal("0.2")),
                         InstrumentWeight(instrument_id="ETH-USD", weight=Decimal("0.3")))
        self.arguments = dict(instrument_id="BTC-USD", previous_targets=self.previous,
                              product_track=ProductTrack.SPOT, decision_id="decision-1", snapshot_id="snapshot-1",
                              allocation_revision=1, expires_at=datetime.now(timezone.utc) + timedelta(seconds=30))

    def test_hold_is_no_proposal_and_explicit_zero_sell_is_a_vector(self) -> None:
        self.assertIsNone(self.adapter.propose(side="HOLD", **self.arguments))
        result = self.adapter.propose(side="BUY", **self.arguments)
        self.assertEqual([entry.weight for entry in result.weights], [Decimal("0.3"), Decimal("0.3")])
        self.assertNotIn("environment", type(result).model_fields)
        self.arguments["previous_targets"] = (
            InstrumentWeight(instrument_id="BTC-USD", weight=Decimal("0.1")),
            InstrumentWeight(instrument_id="ETH-USD", weight=Decimal("0.0")),
        )
        result = self.adapter.propose(side="SELL", **self.arguments)
        self.assertEqual([entry.weight for entry in result.weights], [Decimal(0), Decimal(0)])

    def test_full_allocations_never_sell_another_asset_and_perpetual_is_disabled(self) -> None:
        self.arguments["previous_targets"] = (
            InstrumentWeight(instrument_id="BTC-USD", weight=Decimal("0.5")),
            InstrumentWeight(instrument_id="ETH-USD", weight=Decimal("0.5")),
        )
        self.assertIsNone(self.adapter.propose(side="BUY", **self.arguments))
        self.arguments["product_track"] = ProductTrack.PERPETUAL
        with self.assertRaisesRegex(ValueError, "no qualified profile"):
            self.adapter.propose(side="SELL", **self.arguments)


class NativeCheckpointTests(unittest.TestCase):
    def valid_arrays(self) -> dict:
        n, slots = 4, 19
        return {"queue": np.zeros((slots, n), np.int32), "queue_count": np.zeros(slots, np.int32),
                "active": np.zeros(n, np.int32), "active_flag": np.zeros(n, np.uint8),
                "nactive": np.zeros(1, np.int32), "refractory": np.zeros(n, np.int16),
                "last": np.full(n, -1, np.int64), "eligibility_last": np.zeros(n, np.int64),
                "modulation_last": np.zeros(n, np.int64)}

    def test_valid_state_and_native_pointer_overflow_cases(self) -> None:
        validate_native_state(self.valid_arrays(), neuron_count=4, cursor=0)
        for field, value in [("nactive", 5), ("queue_count", 5), ("refractory", 23),
                             ("last", 100), ("modulation_last", 100)]:
            arrays = self.valid_arrays()
            arrays[field][0] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_native_state(arrays, neuron_count=4, cursor=0)

    def test_active_flag_mismatch_and_duplicate_queue_entries(self) -> None:
        arrays = self.valid_arrays()
        arrays["active_flag"][1] = 1
        with self.assertRaisesRegex(ValueError, "flags disagree"):
            validate_native_state(arrays, neuron_count=4, cursor=0)
        arrays = self.valid_arrays()
        arrays["queue_count"][0] = 2
        with self.assertRaisesRegex(ValueError, "queued neuron"):
            validate_native_state(arrays, neuron_count=4, cursor=0)

    def test_checkpoint_archive_rejects_pickle_and_unexpected_native_layout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.npz"
            expected = {"weight": np.zeros(4, dtype=np.float32)}
            np.savez(path, metadata='{"cursor":0}', **expected)
            validate_checkpoint_archive(path, expected)
            np.savez(path, metadata='{"cursor":0}', weight=np.zeros(400, dtype=np.float32))
            with self.assertRaisesRegex(ValueError, "layout mismatch"):
                validate_checkpoint_archive(path, expected)
            np.savez(path, metadata=np.array({"cursor": 0}, dtype=object), **expected)
            with self.assertRaises(ValueError):
                validate_checkpoint_archive(path, expected)


if __name__ == "__main__":
    unittest.main()
