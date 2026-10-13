"""Thread-safe bounded duration histograms; targets are not claimed measurements."""

import math
import threading
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class MetricSnapshot:
    count: int
    p50_ms: float | None
    p95_ms: float | None
    p99_ms: float | None


class DurationMetric:
    def __init__(self, window: int = 4096) -> None:
        if not 1 <= window <= 100_000:
            raise ValueError("invalid metric window")
        self._samples: deque[float] = deque(maxlen=window)
        self._count = 0
        self._lock = threading.Lock()

    def observe(self, milliseconds: float) -> None:
        if not math.isfinite(milliseconds) or milliseconds < 0:
            raise ValueError("duration must be finite and nonnegative")
        with self._lock:
            self._samples.append(milliseconds)
            self._count += 1

    def snapshot(self) -> MetricSnapshot:
        with self._lock:
            values = sorted(self._samples)
            count = self._count
        if not values:
            return MetricSnapshot(count, None, None, None)

        def percentile(fraction: float) -> float:
            return values[max(0, math.ceil(len(values) * fraction) - 1)]

        return MetricSnapshot(count, percentile(.50), percentile(.95), percentile(.99))
