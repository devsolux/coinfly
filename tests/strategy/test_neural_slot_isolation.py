"""Multi-slot neural-state isolation around one shared graph.

A synthetic brain carries the complete mutable inventory of the real
``VisualMemoryBrain`` (every ``fields`` array including ``r8_light``, the native
counters, plasticity diagnostics, plastic weights) with a deterministic toy
propagator. Isolation is proven by exact comparison: slot A observed alone
must produce exactly the same committed state digests as slot A interleaved
with slot B — in frozen mode and in learning mode — and B's state never leaks
into A. Simultaneous timestamps, retirement/recreation, an exception during a
swap, aliasing, incomplete inventories, frozen-weight mutation and the byte
budget are covered. Nothing here is neural behaviour; it is the state
bookkeeping the neural model runs on.
"""

from __future__ import annotations

import hashlib
import time
from types import SimpleNamespace
from typing import Any
from unittest import TestCase

import numpy as np

from coinfly.domain.context import DomainError
from coinfly.strategy.neural_slots import SharedGraphSlotStates

FIELDS = ["v", "g", "refractory", "drive", "previous_drive", "queue", "queue_count", "counts", "luminance", "active", "active_flag",
          "nactive", "last", "eligibility", "eligibility_last", "modulation", "modulation_last", "adaptation", "rate_kc", "rate_dan",
          "memory_u", "memory_w", "r8_light"]


def synthetic_brain(*, n: int = 64, edges: int = 8, frozen: bool) -> Any:
    slots = 19
    brain = SimpleNamespace(n=n, fields=list(FIELDS), cursor=0, sim_ms=0.0, total_spikes=0, plasticity_diagnostics={},
                            weights_frozen=frozen, dt=0.1)
    shapes: dict[str, tuple[tuple[int, ...], Any]] = {
        "v": ((n,), np.float32), "g": ((n,), np.float32), "refractory": ((n,), np.int16), "drive": ((n,), np.float32),
        "previous_drive": ((n,), np.float32), "queue": ((slots, n), np.int32), "queue_count": ((slots,), np.int32),
        "counts": ((n,), np.int32), "luminance": ((n,), np.float32), "active": ((n,), np.int32), "active_flag": ((n,), np.uint8),
        "nactive": ((1,), np.int32), "last": ((n,), np.int64), "eligibility": ((n,), np.float64), "eligibility_last": ((n,), np.int64),
        "modulation": ((n,), np.float32), "modulation_last": ((n,), np.int64), "adaptation": ((n,), np.float32),
        "rate_kc": ((edges,), np.float64), "rate_dan": ((2,), np.float64), "memory_u": ((edges,), np.float64),
        "memory_w": ((edges,), np.float64), "r8_light": ((n,), np.float32),
    }
    for name, (shape, dtype) in shapes.items():
        setattr(brain, name, np.zeros(shape, dtype=dtype))
    brain.initial = {name: getattr(brain, name).copy() for name in FIELDS}
    brain.weight = np.linspace(0.5, 1.5, 4 * edges).astype(np.float32)
    brain.circuit = {"edges": np.arange(edges)}
    brain.baseline_plastic = brain.weight[brain.circuit["edges"]].copy()

    def step(stimulus: float, *, learning: bool) -> None:
        # Deterministic recurrent dynamics: the next state depends on the current state and the input.
        brain.v[:] = brain.v * 0.9 + stimulus
        brain.g[:] = brain.g + brain.v * 0.01
        brain.counts[:] = (brain.v > 0.5).astype(np.int32)
        brain.active_flag[:] = brain.counts.astype(np.uint8)
        brain.nactive[0] = int(brain.counts.sum())
        brain.last[brain.counts > 0] = brain.cursor
        brain.eligibility[:] = brain.eligibility * 0.5 + brain.counts
        brain.memory_u[:] = brain.memory_u + stimulus * 0.001
        brain.r8_light[:] = stimulus
        brain.cursor += 10
        brain.sim_ms += 1.0
        brain.total_spikes += int(brain.counts.sum())
        brain.plasticity_diagnostics["bins"] = int(brain.plasticity_diagnostics.get("bins", 0)) + 1
        if learning and not brain.weights_frozen:
            brain.memory_w[:] = brain.memory_w + brain.memory_u * 0.1
            brain.weight[brain.circuit["edges"]] = brain.baseline_plastic * (1 + brain.memory_w)
    brain.step = step
    return brain


def run(states: SharedGraphSlotStates, brain: Any, slot: str, stimulus: float, *, learning: bool) -> str:
    states.activate(slot)
    brain.step(stimulus, learning=learning)
    states.commit(slot)
    return states.state_digest(slot)


class IsolationTests(TestCase):
    def sequence(self, *, learning: bool, interleave: bool, steps: int = 6) -> list[str]:
        brain = synthetic_brain(frozen=not learning)
        states = SharedGraphSlotStates(brain, learning)
        states.admit("A")
        if interleave:
            states.admit("B")
        digests = []
        for index in range(steps):
            if interleave:
                run(states, brain, "B", float(index + 7) * 3.3, learning=learning)  # B runs a different story
            digests.append(run(states, brain, "A", float(index + 1), learning=learning))
            if interleave and index % 2:
                run(states, brain, "B", -1.0, learning=learning)
        return digests

    def test_frozen_slot_a_is_bit_identical_alone_and_interleaved(self) -> None:
        alone = self.sequence(learning=False, interleave=False)
        interleaved = self.sequence(learning=False, interleave=True)
        self.assertEqual(alone, interleaved)
        self.assertEqual(len(set(alone)), len(alone))  # the state actually evolves

    def test_learning_slot_a_is_bit_identical_alone_and_interleaved_including_plastic_weights(self) -> None:
        alone = self.sequence(learning=True, interleave=False)
        interleaved = self.sequence(learning=True, interleave=True)
        self.assertEqual(alone, interleaved)
        self.assertNotEqual(alone, self.sequence(learning=False, interleave=False))  # learning changes the digest (weights)

    def test_slots_do_not_share_state_and_the_graph_is_not_cloned(self) -> None:
        brain = synthetic_brain(frozen=False)
        states = SharedGraphSlotStates(brain, True)
        states.admit("A")
        states.admit("B")
        run(states, brain, "A", 5.0, learning=True)
        digest_a = states.state_digest("A")
        run(states, brain, "B", 9.0, learning=True)
        self.assertEqual(states.state_digest("A"), digest_a)
        self.assertNotEqual(states.state_digest("B"), digest_a)
        usage = states.usage()
        self.assertTrue(usage["shared_graph"])
        self.assertFalse(usage["graph_cloned_per_slot"])
        self.assertTrue(usage["plastic_state_per_slot"])
        self.assertEqual(usage["admitted_slots"], 2)
        self.assertEqual(usage["used_bytes"], 2 * usage["per_slot_state_bytes"])
        # Every inventory array is a private copy: no view of the brain or of the other slot.
        for name, array in states._states["A"].arrays.items():
            self.assertFalse(np.shares_memory(array, getattr(brain, name)), name)
            self.assertFalse(np.shares_memory(array, states._states["B"].arrays[name]), name)

    def test_simultaneous_timestamps_and_retirement_recreation(self) -> None:
        brain = synthetic_brain(frozen=True)
        states = SharedGraphSlotStates(brain, False)
        states.admit("A")
        states.admit("B")
        fresh = states.state_digest("B")
        # The same decision instant for both slots: state is keyed by slot, never by time.
        run(states, brain, "A", 1.0, learning=False)
        run(states, brain, "B", 1.0, learning=False)
        self.assertEqual(states.state_digest("A"), states.state_digest("B"))  # same input, same fresh start → same state
        run(states, brain, "B", 2.0, learning=False)
        self.assertNotEqual(states.state_digest("A"), states.state_digest("B"))
        digest_a = states.state_digest("A")
        # Retire B and recreate it: it starts from the brain's initial state, not from its old state, and A is untouched.
        states.release("B")
        self.assertEqual(states.admitted(), ("A",))
        states.admit("B")
        self.assertEqual(states.state_digest("B"), fresh)
        self.assertEqual(states.state_digest("A"), digest_a)
        self.assertEqual(states.generation("B"), 0)

    def test_uncommitted_work_is_disposed_and_discard_restores(self) -> None:
        brain = synthetic_brain(frozen=True)
        states = SharedGraphSlotStates(brain, False)
        states.admit("A")
        states.admit("B")
        committed = run(states, brain, "A", 1.0, learning=False)
        states.activate("A")
        brain.step(50.0, learning=False)  # uncommitted working state
        states.discard("A")
        self.assertEqual(states.state_digest("A"), committed)
        self.assertEqual(states.generation("A"), 1)
        # Activating B while A's work is uncommitted disposes of that work.
        states.activate("A")
        brain.step(50.0, learning=False)
        states.activate("B")
        self.assertEqual(states.state_digest("A"), committed)
        with self.assertRaises(DomainError) as refused:
            states.commit("A")  # A is not the active slot
        self.assertEqual(refused.exception.code, "NEURAL_SLOT_NOT_ACTIVE")
        # Releasing the active slot disposes its working state without saving it.
        states.activate("A")
        brain.step(50.0, learning=False)
        states.release("A")
        self.assertNotIn("A", states.admitted())

    def test_exception_during_swap_leaves_committed_states_intact(self) -> None:
        brain = synthetic_brain(frozen=True)
        states = SharedGraphSlotStates(brain, False)
        states.admit("A")
        states.admit("B")
        committed_a = run(states, brain, "A", 1.0, learning=False)
        committed_b = run(states, brain, "B", 2.0, learning=False)
        # Corrupt the brain's array for one field so the next swap fails mid-way.
        good = brain.g
        brain.g = np.zeros(3, dtype=np.float32)
        with self.assertRaises(ValueError):
            states.activate("A")
        brain.g = good
        self.assertEqual(states.state_digest("A"), committed_a)
        self.assertEqual(states.state_digest("B"), committed_b)
        # The half-swapped brain belongs to no slot: nothing can commit it.
        with self.assertRaises(DomainError) as refused:
            states.commit("B")
        self.assertEqual(refused.exception.code, "NEURAL_SLOT_NOT_ACTIVE")
        states.activate("A")
        states.discard("A")
        self.assertEqual(states.state_digest("A"), committed_a)

    def test_frozen_weights_are_verified_around_every_observation(self) -> None:
        brain = synthetic_brain(frozen=True)
        states = SharedGraphSlotStates(brain, False)
        states.admit("A")
        run(states, brain, "A", 1.0, learning=False)
        brain.weight[0] += 1.0  # something mutated the shared weights while frozen
        with self.assertRaises(DomainError) as refused:
            states.activate("A")
        self.assertEqual(refused.exception.code, "NEURAL_WEIGHTS_MUTATED_WHILE_FROZEN")
        unfrozen = synthetic_brain(frozen=False)
        with self.assertRaises(DomainError) as not_frozen:
            SharedGraphSlotStates(unfrozen, False)
        self.assertEqual(not_frozen.exception.code, "NEURAL_WEIGHTS_NOT_FROZEN")

    def test_inventory_is_asserted(self) -> None:
        brain = synthetic_brain(frozen=True)
        brain.fields.remove("queue")
        with self.assertRaises(DomainError) as incomplete:
            SharedGraphSlotStates(brain, False)
        self.assertEqual(incomplete.exception.code, "NEURAL_STATE_INVENTORY_INCOMPLETE")
        aliased = synthetic_brain(frozen=True)
        aliased.previous_drive = aliased.drive  # two names, one array
        with self.assertRaises(DomainError) as alias:
            SharedGraphSlotStates(aliased, False)
        self.assertEqual(alias.exception.code, "NEURAL_STATE_ALIASED")
        missing = synthetic_brain(frozen=True)
        del missing.cursor
        with self.assertRaises(DomainError):
            SharedGraphSlotStates(missing, False)

    def test_memory_budget_admits_explicitly(self) -> None:
        brain = synthetic_brain(frozen=True)
        per_slot = sum(int(getattr(brain, name).nbytes) for name in FIELDS)
        states = SharedGraphSlotStates(brain, False, budget_bytes=per_slot * 2 + 1)
        self.assertEqual(states.per_slot_bytes, per_slot)
        self.assertEqual(states.capacity, 2)
        states.admit("A")
        states.admit("B")
        with self.assertRaises(DomainError) as refused:
            states.admit("C")
        self.assertEqual(refused.exception.code, "NEURAL_STATE_BUDGET_EXCEEDED")
        self.assertEqual(states.admitted(), ("A", "B"))
        learning = synthetic_brain(frozen=False)
        learning_states = SharedGraphSlotStates(learning, True, budget_bytes=per_slot * 2 + 1)
        self.assertGreater(learning_states.per_slot_bytes, per_slot)  # plastic weights are part of the per-slot state
        self.assertEqual(learning_states.capacity, 1)

    def test_full_size_swap_latency_is_bounded(self) -> None:
        # The real inventory size (166,700 neurons); a swap must stay far below a decision interval.
        brain = synthetic_brain(n=166_700, edges=2048, frozen=True)
        states = SharedGraphSlotStates(brain, False)
        states.admit("A")
        states.admit("B")
        started = time.perf_counter()
        for _ in range(10):
            states.activate("A")
            states.commit("A")
            states.activate("B")
            states.commit("B")
        per_swap = (time.perf_counter() - started) / 40
        self.assertLess(per_swap, 0.25, f"swap took {per_swap * 1000:.1f} ms")
        self.assertEqual(hashlib.sha256(np.ascontiguousarray(brain.weight).tobytes()).hexdigest(), states.usage()["frozen_weight_sha256"])
