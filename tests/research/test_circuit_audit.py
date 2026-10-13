"""Synthetic annotation/CSR tests; the real MaleCNS lock manifests stay intact."""

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from coinfly.research.neural import circuit


@pytest.mark.parametrize("contact", [1., 2e38])
def test_compartment_gains_are_finite_and_topology_is_unchanged(monkeypatch, contact):
    types = ["PAM11"] * 15 + ["PPL101"] * 2 + ["MBON07"] * 4 + ["MBON11"] * 2 + ["KC"] * 2
    annotation = pd.DataFrame({"type": types, "instance": ["synthetic"] * len(types)})
    monkeypatch.setattr(circuit, "annotations", lambda *_: annotation)
    ptr, post, weight = [0], [], []
    for i in range(len(types)):
        targets = range(17, 21) if i < 15 else range(21, 23) if i < 17 else range(17, 23) if i >= 23 else [i]
        for target in targets:
            # Duplicate contacts reproduce float32 accumulation overflow while
            # retaining every connection, including the small self connections.
            for _ in range(2):
                post.append(target)
                weight.append(contact if i < 17 else .001)
        ptr.append(len(post))
    brain = SimpleNamespace(n=len(types), ids=np.arange(len(types), dtype=np.int64),
                            ptr=np.array(ptr, np.int64), post=np.array(post, np.int32),
                            weight=np.array(weight, np.float32),
                            paths=SimpleNamespace(annotations=Path("synthetic-not-read.feather")))
    before = {key: getattr(brain, key).copy() for key in ("ids", "ptr", "post", "weight")}
    result = circuit.identify(brain)
    assert np.isfinite(result["gain"]).all()
    np.testing.assert_allclose(result["gain"].sum(axis=0), 1, rtol=1e-6)
    assert len(result["edges"]) == 24
    for key, expected in before.items():
        np.testing.assert_array_equal(getattr(brain, key), expected)
