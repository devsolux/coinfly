"""Rebuild the preserved full graph from checksum-verified MaleCNS sources.

This is the archived v1 preparation algorithm with explicit paths and cancellation.
The caller must verify all source bytes first and all derived array locks afterward.
"""

from pathlib import Path
from typing import Any

import numpy as np

from coinfly.research.neural.connectome import exact_ids, index_edges, normalize_nodes
from coinfly.research.neural.transmitters import transmitter_signs

from .installer import InstallControls, ProgressCallback
from .manifest import BootstrapError


def prepare_graph(directory: Path, controls: InstallControls, progress: ProgressCallback) -> None:
    import pyarrow as pa
    import pyarrow.feather as feather
    import pyarrow.ipc as ipc

    controls.checkpoint()
    progress("PREPARING", "normalized/neurons.feather", 0, 0)
    annotations = feather.read_table(directory / "annotations.feather").to_pandas()
    transmitters = feather.read_table(directory / "neurotransmitters.feather").to_pandas()
    _, nodes = normalize_nodes("malecns_v1", annotations, transmitters)
    if len(nodes) != 166700:
        raise BootstrapError("NEURON_COUNT_MISMATCH")
    feather.write_feather(nodes, directory / "normalized/neurons.feather")
    ids = exact_ids(nodes.source_id)
    parts: list[tuple[Any, Any, Any]] = []
    with pa.memory_map(str(directory / "edges.feather"), "r") as source:
        reader = ipc.open_file(source)
        for number in range(reader.num_record_batches):
            controls.checkpoint()
            batch = reader.get_batch(number)
            pre, post, count = [batch.column(batch.schema.get_field_index(name)).to_numpy(zero_copy_only=False)
                                for name in ("body_pre", "body_post", "weight")]
            pre_indices, post_indices, counts, _ = index_edges(ids, pre, post, count)
            parts.append((pre_indices, post_indices, counts))
            progress("PREPARING", "edges.feather", 0, 0)
    pre, post, count = [np.concatenate([part[column] for part in parts]) for column in range(3)]
    del parts
    if len(pre) != 25582938:
        raise BootstrapError("GRAPH_EDGE_COUNT_MISMATCH")
    controls.checkpoint()
    progress("PREPARING", "graph.npz", 0, 0)
    order = np.argsort(pre, kind="stable")
    ptr = np.r_[0, np.cumsum(np.bincount(pre, minlength=len(nodes)))].astype(np.int64)
    signs, _ = transmitter_signs(nodes.neurotransmitter)
    weight = (count[order].astype(np.float32) * signs[pre[order]] * 0.275).astype(np.float32)
    annotations = annotations.set_index("bodyId").loc[nodes.source_id]
    receptors = annotations.type.eq("R1-R6").to_numpy()
    anchors = annotations.type.isin(["L1", "L2", "L3"]).to_numpy() & annotations.assignedOlHex1.notna().to_numpy()
    selected = receptors[pre] & anchors[post]
    columns: dict[int, dict[tuple[float, float], int]] = {}
    for i, j, value in zip(pre[selected], post[selected], count[selected]):
        key = (float(annotations.assignedOlHex1.iloc[j]), float(annotations.assignedOlHex2.iloc[j]))
        contacts = columns.setdefault(int(i), {})
        contacts[key] = contacts.get(key, 0) + int(value)
    indices, xy, confidence, hexes = [], [], [], []
    for i, contacts in sorted(columns.items()):
        h = max(contacts, key=lambda key: contacts[key])
        indices.append(i)
        hexes.append(h)
        confidence.append(contacts[h] / sum(contacts.values()))
        xy.append((h[0] - 0.5 * h[1], np.sqrt(3) / 2 * h[1]))
    coordinates = np.asarray(xy)
    retina = np.asarray(indices, dtype=np.int32)
    uv = np.empty_like(coordinates)
    sides = annotations.rootSide.to_numpy()[retina]
    if len(retina) != 3335 or not np.isin(sides, ["L", "R"]).all():
        raise BootstrapError("RETINAL_PROJECTION_INVALID")
    for side in ["L", "R"]:
        mask = sides == side
        z = coordinates[mask]
        z = (z - z.min(axis=0)) / (z.max(axis=0) - z.min(axis=0))
        uv[mask, 0] = 0.60 * z[:, 0] if side == "L" else 0.40 + 0.60 * (1 - z[:, 0])
        uv[mask, 1] = 1 - z[:, 1]
    controls.checkpoint()
    np.savez(directory / "graph.npz", ptr=ptr, post=post[order].astype(np.int32), weight=weight,
             ids=nodes.source_id.to_numpy(dtype=np.int64), retina=retina, uv=uv.astype(np.float32),
             confidence=np.asarray(confidence), hexes=np.asarray(hexes),
             lamina=np.flatnonzero(nodes.cell_type.isin(["L1", "L2", "L3", "L5"]).to_numpy()).astype(np.int32),
             sugar=np.flatnonzero(nodes.cell_type.eq("LB3c").to_numpy()).astype(np.int32),
             superclass=np.asarray(nodes.superclass.fillna("unassigned").astype(str), dtype="U64"))
    controls.checkpoint()
