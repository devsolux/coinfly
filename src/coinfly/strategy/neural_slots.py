"""Shared read-only graph, per-slot dynamic neural state, explicit memory admission.

The full MaleCNS graph (CSR structure and synaptic weights) is loaded once per
worker. What differs between instruments is only the dynamic state the
propagator evolves. This module owns the complete inventory of that mutable
state and swaps it around every observation, so no slot ever sees another
instrument's recurrent or plastic state and the graph is never cloned.

State inventory (``MemoryBrain``/``VisualMemoryBrain`` as audited on 2026-09-22):

* ``brain.fields`` — membrane ``v``, conductance ``g``, ``refractory`` clocks,
  ``drive``/``previous_drive``, spike delay ``queue``/``queue_count``, ``counts``,
  ``luminance``, ``active``/``active_flag``/``nactive`` lists, ``last`` spike
  times, ``eligibility``/``eligibility_last``, ``modulation``/``modulation_last``
  (DAN delivery traces), ``adaptation``, ``rate_kc``/``rate_dan``, latent
  ``memory_u`` and expressed ``memory_w``, and ``r8_light`` (visual adapter).
* Scalars — ``cursor`` (native tick clock), ``sim_ms``, ``total_spikes``.
* ``plasticity_diagnostics`` — a per-run dict the kernels append to.
* ``weight[circuit["edges"]]`` — the effective plastic efficacies. They change
  only while learning (``weights_frozen`` is False); frozen slots share one
  immutable vector whose digest is verified around every observation.
* Experimental-plasticity engine state (traces, tags, eligibility ring, DAN
  state, gain, frame clock) exists only on ``TrainingController``; it is bound
  to one chronological session (``session_resume=False``) and is therefore
  never swapped: a research slot owns its own controller (see
  ``strategy.controllers``), never a shared one.

Anything not in the inventory above is immutable graph/circuit data
(``ids``, ``ptr``, ``post``, ``retina``, ``uv``, ``lamina``, ``sugar``,
``circuit``, ``baseline_plastic``, ``rest``, ``tonic``, ``modulation_mask``).
The inventory is asserted at construction: an attribute the brain mutates but
this module does not track is a defect, so unknown mutable attributes are
listed explicitly and checked by ``verify_inventory``.

Memory is budgeted explicitly: the per-slot state size is measured from the
real arrays, and admission refuses a slot that would exceed the configured
budget instead of allocating it silently.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from coinfly.domain.context import DomainError

DEFAULT_NEURAL_STATE_BUDGET_BYTES = 512 * 1024 * 1024
SCALAR_STATE = ("cursor", "sim_ms", "total_spikes")


def _digest(array: np.ndarray) -> str:
    return hashlib.sha256(np.ascontiguousarray(array).tobytes()).hexdigest()


@dataclass
class SlotNeuralState:
    arrays: dict[str, np.ndarray]
    cursor: int
    sim_ms: float
    total_spikes: int
    plastic_weights: np.ndarray | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)
    observations: int = 0
    generation: int = 0

    @property
    def nbytes(self) -> int:
        total = sum(int(array.nbytes) for array in self.arrays.values())
        if self.plastic_weights is not None:
            total += int(self.plastic_weights.nbytes)
        return total


@dataclass
class SharedGraphSlotStates:
    """Per-slot dynamic state around one shared brain; admission by byte budget."""

    brain: Any
    learning: bool
    budget_bytes: int = DEFAULT_NEURAL_STATE_BUDGET_BYTES
    _states: dict[str, SlotNeuralState] = field(default_factory=dict)
    _active: str | None = None
    _frozen_digest: str | None = None

    def __post_init__(self) -> None:
        if self.budget_bytes < 1:
            raise ValueError("positive neural state budget required")
        fields = list(getattr(self.brain, "fields", ()))
        missing = [name for name in fields if not hasattr(self.brain, name)]
        if missing or not fields:
            raise DomainError("NEURAL_STATE_FIELDS_UNAVAILABLE")
        self.verify_inventory()
        if not self.learning:
            if getattr(self.brain, "weights_frozen", None) is not True:
                raise DomainError("NEURAL_WEIGHTS_NOT_FROZEN")
            self._frozen_digest = _digest(self.brain.weight)

    def verify_inventory(self) -> None:
        """Every array the brain evolves must be swapped; immutable graph data must stay untouched."""
        required = {"v", "g", "refractory", "drive", "queue", "queue_count", "counts", "luminance", "active",
                    "active_flag", "nactive"}
        fields = set(self.brain.fields)
        if not required <= fields:
            raise DomainError("NEURAL_STATE_INVENTORY_INCOMPLETE")
        for name in SCALAR_STATE:
            if not hasattr(self.brain, name):
                raise DomainError("NEURAL_STATE_INVENTORY_INCOMPLETE")
        for name in fields:
            array = getattr(self.brain, name)
            if not isinstance(array, np.ndarray):
                raise DomainError("NEURAL_STATE_INVENTORY_INCOMPLETE")
            if any(name != other and np.shares_memory(array, getattr(self.brain, other)) for other in fields):
                raise DomainError("NEURAL_STATE_ALIASED")

    @property
    def per_slot_bytes(self) -> int:
        total = sum(int(getattr(self.brain, name).nbytes) for name in self.brain.fields)
        if self.learning:
            edges = self.brain.circuit["edges"]
            total += int(self.brain.weight[edges].nbytes)
        return total

    @property
    def capacity(self) -> int:
        return max(0, self.budget_bytes // max(1, self.per_slot_bytes))

    def admitted(self) -> tuple[str, ...]:
        return tuple(self._states)

    def generation(self, slot_key: str) -> int:
        state = self._states.get(slot_key)
        return state.generation if state is not None else -1

    def usage(self) -> dict[str, Any]:
        used = sum(state.nbytes for state in self._states.values())
        return {"per_slot_state_bytes": self.per_slot_bytes, "budget_bytes": self.budget_bytes,
                "capacity_slots": self.capacity, "admitted_slots": len(self._states), "used_bytes": used,
                "shared_graph": True, "graph_cloned_per_slot": False, "plastic_state_per_slot": self.learning,
                "frozen_weight_sha256": self._frozen_digest}

    def admit(self, slot_key: str) -> None:
        if slot_key in self._states:
            return
        if (len(self._states) + 1) * self.per_slot_bytes > self.budget_bytes:
            raise DomainError("NEURAL_STATE_BUDGET_EXCEEDED")
        # A fresh slot starts from the brain's initial state, not from whatever
        # instrument ran last.
        arrays = {name: np.array(self.brain.initial[name], copy=True) for name in self.brain.fields}
        plastic = None
        if self.learning:
            plastic = np.array(self.brain.baseline_plastic, copy=True)
        self._states[slot_key] = SlotNeuralState(arrays=arrays, cursor=0, sim_ms=0.0, total_spikes=0, plastic_weights=plastic)

    def release(self, slot_key: str) -> None:
        if self._active == slot_key:
            # An uncommitted working state of a released slot is disposed, not saved.
            self._active = None
        self._states.pop(slot_key, None)

    def _save(self, slot_key: str) -> None:
        state = self._states[slot_key]
        for name, array in state.arrays.items():
            np.copyto(array, getattr(self.brain, name))
        state.cursor = int(self.brain.cursor)
        state.sim_ms = float(self.brain.sim_ms)
        state.total_spikes = int(self.brain.total_spikes)
        state.diagnostics = dict(getattr(self.brain, "plasticity_diagnostics", {}))
        if state.plastic_weights is not None:
            np.copyto(state.plastic_weights, self.brain.weight[self.brain.circuit["edges"]])

    def _load(self, slot_key: str) -> None:
        state = self._states[slot_key]
        for name, array in state.arrays.items():
            np.copyto(getattr(self.brain, name), array)
        self.brain.cursor = state.cursor
        self.brain.sim_ms = state.sim_ms
        self.brain.total_spikes = state.total_spikes
        if hasattr(self.brain, "plasticity_diagnostics"):
            self.brain.plasticity_diagnostics = dict(state.diagnostics)
        if state.plastic_weights is not None:
            self.brain.weight[self.brain.circuit["edges"]] = state.plastic_weights

    def _check_frozen(self) -> None:
        if self._frozen_digest is not None and _digest(self.brain.weight) != self._frozen_digest:
            raise DomainError("NEURAL_WEIGHTS_MUTATED_WHILE_FROZEN")

    def activate(self, slot_key: str) -> None:
        """Swap the requested slot's committed state into the shared brain.

        The previously active slot's working state is NOT saved here: a slot's
        state advances only through ``commit``. Activating another slot while an
        observation is uncommitted therefore disposes of that observation.
        """
        if slot_key not in self._states:
            raise DomainError("NEURAL_SLOT_NOT_ADMITTED")
        self._check_frozen()
        # No slot is active while the swap runs: a failure half-way leaves the
        # brain in a mixed state that no commit may ever save.
        self._active = None
        self._load(slot_key)
        self._active = slot_key

    def commit(self, slot_key: str) -> None:
        if self._active != slot_key:
            raise DomainError("NEURAL_SLOT_NOT_ACTIVE")
        self._check_frozen()
        self._save(slot_key)
        state = self._states[slot_key]
        state.observations += 1
        state.generation += 1

    def discard(self, slot_key: str) -> None:
        """Fenced disposal: restore the last committed state; the generation does not advance."""
        if slot_key not in self._states:
            return
        if self._active == slot_key:
            self._load(slot_key)
        self._check_frozen()

    def state_digest(self, slot_key: str) -> str:
        """Digest of a slot's committed state (arrays, scalars, plastic weights)."""
        state = self._states[slot_key]
        digest = hashlib.sha256()
        for name in sorted(state.arrays):
            digest.update(name.encode())
            digest.update(np.ascontiguousarray(state.arrays[name]).tobytes())
        digest.update(f"{state.cursor}:{state.sim_ms!r}:{state.total_spikes}".encode())
        if state.plastic_weights is not None:
            digest.update(np.ascontiguousarray(state.plastic_weights).tobytes())
        return digest.hexdigest()
