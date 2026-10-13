"""The separately versioned settings of the preserved neural experiment."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class NeuralProfile:
    profile_id: str = "coinfly-dual-compartment-v1"
    neural_ms: float = 500.0
    neural_bin_ms: float = 10.0
    pulse_ms: float = 200.0
    pulse_current: float = 20.0
    decoder_threshold_hz: float = 2.0
    learning: bool = True

    def __post_init__(self) -> None:
        if self.profile_id != "coinfly-dual-compartment-v1" or type(self.learning) is not bool:
            raise ValueError("unknown neural profile")
        values = [self.neural_ms, self.neural_bin_ms, self.pulse_ms,
                  self.pulse_current, self.decoder_threshold_hz]
        if any(isinstance(value, bool) or not math.isfinite(value) or value <= 0 for value in values):
            raise ValueError("positive finite neural parameters required")
        if self.neural_ms > 60_000 or self.neural_bin_ms > 10 or self.pulse_ms > self.neural_ms:
            raise ValueError("neural pulse/bin duration outside declared bounds")
        if any(value < 0.1 or abs(value * 10 - round(value * 10)) > 1e-7
               for value in [self.neural_ms, self.neural_bin_ms, self.pulse_ms]):
            raise ValueError("neural intervals must be multiples of 0.1 ms")
