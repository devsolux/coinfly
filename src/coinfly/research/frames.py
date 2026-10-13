"""The audited sensory frame handed to the experimental controller (no Polars needed here).

``coinfly.research.causal_data`` builds these from long-form feature tables;
the worker builds them from its own exact sensory transform. Both hand the
controller an owned, hash-verified RGB frame with explicit causal evidence.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from coinfly.research.reward import ObservationEvidence
from coinfly.research.vectorization import TensorBatch


@dataclass(frozen=True)
class AuditedFrame:
    rgb: NDArray[np.uint8]
    tensor: TensorBatch
    evidence: ObservationEvidence
    sha256: str

    def owned_rgb(self) -> NDArray[np.uint8]:
        frame = np.array(self.rgb, copy=True, order="C")
        if hashlib.sha256(frame.tobytes()).hexdigest() != self.sha256:
            raise ValueError("audited frame was mutated")
        frame.flags.writeable = False
        return frame
