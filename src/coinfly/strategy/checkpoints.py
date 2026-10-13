"""Explicit, provenance-checked checkpoint loading for the preserved controller.

A checkpoint is loaded only when the strategy names it (``FROZEN_CHECKPOINT``)
and only if its declared provenance matches: the graph digests, build, rule
parameters and configuration signature (checked by ``brain.restore``), frozen
weights, a non-learning controller profile, and the training profile the
strategy expects (v2/v3 are different experiments; neither is loaded silently).
Nothing here creates fresh weights or picks "the latest" checkpoint.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np

from coinfly.domain.context import DomainError
from coinfly.research.checkpoint import validate_checkpoint_archive

CHECKPOINT_SUFFIX = ".npz"


def checkpoint_path(directory: Path, checkpoint_id: str) -> Path:
    if "/" in checkpoint_id or "\\" in checkpoint_id or checkpoint_id in (".", ".."):
        raise DomainError("CHECKPOINT_ID_INVALID")
    path = directory / (checkpoint_id if checkpoint_id.endswith(CHECKPOINT_SUFFIX) else checkpoint_id + CHECKPOINT_SUFFIX)
    if path.is_symlink() or not path.is_file():
        raise DomainError("CHECKPOINT_NOT_FOUND", f"CHECKPOINT_NOT_FOUND: {path.name}")
    return path


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_checkpoint(path: Path, brain: Any) -> dict[str, Any]:
    """Bounded read of the checkpoint's declared metadata (arrays are not materialized)."""
    expected = {name: getattr(brain, name) for name in ["weight", *brain.fields]}
    try:
        validate_checkpoint_archive(path, expected)
        with np.load(path, allow_pickle=False) as archive:
            metadata = json.loads(str(archive["metadata"]))
    except (ValueError, OSError, KeyError) as error:
        raise DomainError("CHECKPOINT_INVALID", f"CHECKPOINT_INVALID: {error}") from None
    if not isinstance(metadata, dict) or not {"cursor", "total_spikes", "weights_frozen", "controller_profile"} <= metadata.keys():
        raise DomainError("CHECKPOINT_INVALID", "CHECKPOINT_INVALID: metadata incomplete")
    return metadata


def restore_frozen_checkpoint(fly_controller: Any, path: Path, *, expected_training_profile_id: str | None) -> dict[str, Any]:
    """Restore a frozen checkpoint into a non-learning controller; return its identity.

    The checkpoint's own ``controller_profile`` is handed to ``brain.restore`` so
    a frozen evaluation checkpoint written by the experimental controller (its
    profile carries the experiment and reward sections) restores under the
    same provenance checks as a plain one. Its neural parameters must equal the
    running profile, its weights must be frozen, and its experiment (if any)
    must be the one the strategy declared.
    """
    brain = fly_controller.brain
    metadata = inspect_checkpoint(path, brain)
    declared = metadata["controller_profile"]
    if not isinstance(declared, dict):
        raise DomainError("CHECKPOINT_PROFILE_MISSING")
    running = asdict(fly_controller.s)
    neural_part = {key: declared.get(key) for key in running}
    if neural_part != running:
        raise DomainError("CHECKPOINT_NEURAL_PROFILE_MISMATCH")
    if metadata.get("weights_frozen") is not True or declared.get("learning") is not False:
        raise DomainError("CHECKPOINT_NOT_FROZEN")
    experiment = declared.get("experiment")
    profile_id = experiment.get("profile_id") if isinstance(experiment, dict) else None
    if profile_id != expected_training_profile_id:
        raise DomainError("CHECKPOINT_PROFILE_MISMATCH",
                          f"CHECKPOINT_PROFILE_MISMATCH: checkpoint declares {profile_id!r}, strategy expects {expected_training_profile_id!r}")
    try:
        brain.restore(path, controller_profile=declared)
    except ValueError as error:
        raise DomainError("CHECKPOINT_PROVENANCE_MISMATCH", f"CHECKPOINT_PROVENANCE_MISMATCH: {error}") from None
    fly_controller._failed = False
    if brain.weights_frozen is not True:
        raise DomainError("CHECKPOINT_NOT_FROZEN")
    return {
        "kind": "FROZEN_CHECKPOINT", "checkpoint_id": path.name, "file_sha256": _file_sha256(path),
        "training_profile_id": profile_id,
        "implementation_revision": experiment.get("implementation_revision") if isinstance(experiment, dict) else None,
        "reward": declared.get("reward"), "cursor": int(metadata["cursor"]), "total_spikes": int(metadata["total_spikes"]),
        "plastic_weights_sha256": hashlib.sha256(np.ascontiguousarray(brain.weight[brain.circuit["edges"]]).tobytes()).hexdigest(),
    }
