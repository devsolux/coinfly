"""Display brightness proxy; not a calibrated compound-eye model."""

from typing import Any, cast

import numpy as np
from numpy.typing import NDArray


def retinal_samples(frame: NDArray[Any], uv: NDArray[Any]) -> NDArray[np.float32]:
    frame = np.asarray(frame)
    if frame.ndim != 3 or frame.shape[2] != 3 or frame.dtype != np.uint8:
        raise ValueError("RGB uint8 frame required")
    if min(frame.shape[:2]) < 1 or uv.ndim != 2 or uv.shape[1] != 2 or not np.isfinite(uv).all() or np.any(uv < 0) or np.any(uv > 1):
        raise ValueError("Nonempty frame and finite normalized UV coordinates required")
    h, w = frame.shape[:2]
    x = np.clip((uv[:, 0] * (w - 1)).astype(int), 0, w - 1)
    y = np.clip((uv[:, 1] * (h - 1)).astype(int), 0, h - 1)
    rgb = frame[y, x].astype(np.float32) / 255
    linear = np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)
    return cast(NDArray[np.float32],
                (linear @ np.asarray([0.2126, 0.7152, 0.0722], dtype=np.float32)).astype(np.float32))
