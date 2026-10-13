"""Loss-accounted import of the retained MaleCNS v1.0 graph for Coinfly.

This module stores topology and annotations. It does not infer missing dynamics,
neurotransmitter receptors, muscle mappings, or behavior from a wiring graph.
"""

import hashlib
from pathlib import Path
from typing import Any

import numpy as np

REGISTRY = Path(__file__).with_name("datasets.json")


def exact_ids(values: Any) -> np.ndarray:
    """Never round 64-bit biological IDs through floating point or JavaScript."""
    items = np.asarray(values)
    if items.dtype.kind == "f":
        raise ValueError(
            "Neuron IDs must be integers or decimal strings, never floats."
        )
    if items.dtype.kind in "iu":
        if np.any(items < 0):
            raise ValueError("Neuron IDs cannot be negative.")
        return items.astype(np.uint64)
    text = [str(value) for value in items]
    if any(not value.isascii() or not value.isdecimal() for value in text):
        raise ValueError("Neuron IDs must be nonnegative decimal integers.")
    return np.asarray(text, dtype=np.uint64)


def index_edges(ids: np.ndarray, pre: Any, post: Any, counts: Any) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Return retained edges plus a mask accounting for every excluded row."""
    if not len(ids) or np.any(ids[1:] <= ids[:-1]):
        raise ValueError("Node IDs must be nonempty, unique, and sorted.")
    pre, post = exact_ids(pre), exact_ids(post)
    counts = np.asarray(counts)
    if len(pre) != len(post) or len(pre) != len(counts):
        raise ValueError("Edge columns have different lengths.")
    if (
        not np.all(np.isfinite(counts))
        or np.any(counts < 1)
        or np.any(counts != np.floor(counts))
        or np.any(counts > 2**32 - 1)
    ):
        raise ValueError("Synapse counts must be positive uint32-compatible integers.")
    i, j = np.searchsorted(ids, pre), np.searchsorted(ids, post)
    keep = (i < len(ids)) & (j < len(ids))
    keep &= ids[np.minimum(i, len(ids) - 1)] == pre
    keep &= ids[np.minimum(j, len(ids) - 1)] == post
    return (
        i[keep].astype(np.uint32),
        j[keep].astype(np.uint32),
        counts[keep].astype(np.uint32),
        keep,
    )


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_nodes(dataset_id: str, frame: Any, nt_frame: Any = None) -> tuple[Any, Any]:
    """Normalize fields while keeping the original annotations in raw files."""
    import pandas as pd

    if dataset_id == "malecns_v1":
        source = exact_ids(frame.bodyId)
        retain = frame.superclass.notna() & frame.superclass.astype(str).ne("")
        quality = frame.statusLabel.astype(object).fillna("unknown")
        superclasses, types = frame.superclass, frame.type
        nonneural = frame.status.eq("Glia")
        retain = retain & ~nonneural
        predicted = pd.Series([None] * len(frame))
        nt_source = pd.Series(["missing"] * len(frame))
        if nt_frame is not None:
            nt_frame = nt_frame.set_index("body")
            if not nt_frame.index.is_unique:
                raise ValueError("Duplicate male neurotransmitter IDs.")
            predicted = frame.bodyId.map(nt_frame.consensus_nt)
            nt_source = pd.Series(
                ["source_consensus_prediction_or_ground_truth"] * len(frame)
            )
        candidate_reason = np.where(
            retain, "assigned_neuronal_superclass", "unresolved_object"
        )
    else:
        raise ValueError(f"Unsupported dataset: {dataset_id}")
    if len(np.unique(source)) != len(source):
        raise ValueError("Duplicate versioned neuron IDs in source annotations.")
    catalog = pd.DataFrame(
        {
            "source_id": source,
            "retained": np.asarray(retain, dtype=bool),
            "object_kind": np.where(
                nonneural,
                "non_neuronal",
                np.where(retain, "neuron_candidate", "unresolved_object"),
            ),
            "inclusion_reason": np.where(
                nonneural, "explicit_non_neuronal_annotation", candidate_reason
            ),
            "quality": np.asarray(quality),
            "superclass": np.asarray(superclasses),
            "cell_type": np.asarray(types),
            "neurotransmitter": np.asarray(predicted),
            "neurotransmitter_source": np.asarray(nt_source),
        }
    ).sort_values("source_id", ignore_index=True)
    nodes = catalog.loc[catalog.retained].reset_index(drop=True)
    nodes.insert(0, "node_index", np.arange(len(nodes), dtype=np.uint32))
    return catalog, nodes

