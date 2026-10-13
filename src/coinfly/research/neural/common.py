"""Data paths are explicit verified inputs; this module reads no environment."""

import hashlib
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray


def digest(array: NDArray[Any]) -> str:
    return hashlib.sha256(array.tobytes()).hexdigest()


def annotations(ids: NDArray[Any], path: Path) -> Any:
    import pyarrow.feather as feather

    frame = feather.read_table(path).to_pandas().set_index("bodyId")
    if not frame.index.is_unique or not np.isin(ids, frame.index.to_numpy()).all():
        raise ValueError("annotation identity is not unique and complete")
    return frame.loc[ids]
