"""Declared neurotransmitter-sign proxy used in the preserved Coinfly profile graph preparation."""

from collections.abc import Iterable

import numpy as np
from numpy.typing import NDArray


def transmitter_signs(transmitters: Iterable[object], ambiguous_sign: int = 1) -> tuple[NDArray[np.int8], NDArray[np.bool_]]:
    """Declared coarse fast-transmission assumption; never deletes unknown edges.

    ACh +; GABA, glutamate, histamine -. A co-transmitter combination with only
    one fast sign uses that sign; conflicting, missing and modulator-only cells
    use the explicit sensitivity parameter. This is NOT receptor physiology.
    """
    if type(ambiguous_sign) is not int or ambiguous_sign not in (-1, 1):
        raise ValueError("Ambiguous edges must remain active with sign +1 or -1.")
    signs, uncertain = [], []
    for value in transmitters:
        tokens = set(str(value).lower().split(","))
        fast = ({1} if "acetylcholine" in tokens else set()) | (
            {-1} if tokens & {"gaba", "glutamate", "histamine"} else set()
        )
        ambiguous = len(fast) != 1
        signs.append(ambiguous_sign if ambiguous else next(iter(fast)))
        uncertain.append(ambiguous)
    return np.asarray(signs, dtype=np.int8), np.asarray(uncertain, dtype=bool)
