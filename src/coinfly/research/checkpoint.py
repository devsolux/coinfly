"""Reject unsafe native state before any checkpoint array enters the kernel."""

from __future__ import annotations

import math
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

Array = NDArray[Any]


def validate_checkpoint_archive(path: Path, expected: Mapping[str, Array]) -> None:
    """Bound decompression and NPY allocation before numpy materializes state."""
    if path.is_symlink() or not path.is_file():
        raise ValueError("invalid checkpoint file")
    allowed = {name + ".npy" for name in expected} | {"metadata.npy"}
    budget = sum(array.nbytes for array in expected.values()) + 256 * 1024
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if (len(members) != len(allowed) or {member.filename for member in members} != allowed
                    or sum(member.file_size for member in members) > budget
                    or any(member.flag_bits & 1 for member in members)):
                raise ValueError("checkpoint archive contract mismatch")
            for member in members:
                with archive.open(member) as source:
                    version = np.lib.format.read_magic(source)
                    if version == (1, 0):
                        shape, fortran, dtype = np.lib.format.read_array_header_1_0(source, max_header_size=16384)
                    elif version == (2, 0):
                        shape, fortran, dtype = np.lib.format.read_array_header_2_0(source, max_header_size=16384)
                    else:
                        raise ValueError("unsupported checkpoint array format")
                    data_bytes = math.prod(shape) * dtype.itemsize
                    if dtype.hasobject or fortran or data_bytes != member.file_size - source.tell():
                        raise ValueError("unsafe checkpoint array header")
                    if member.filename == "metadata.npy":
                        if shape != () or dtype.kind not in "US" or data_bytes > 128 * 1024:
                            raise ValueError("unbounded checkpoint metadata")
                    else:
                        original = expected[member.filename[:-4]]
                        if shape != original.shape or dtype != original.dtype:
                            raise ValueError("checkpoint layout mismatch")
    except (OSError, zipfile.BadZipFile):
        raise ValueError("invalid checkpoint archive") from None


def validate_native_state(arrays: Mapping[str, Array], *, neuron_count: int,
                          cursor: int, dt_ms: float = 0.1) -> None:
    if type(cursor) is not int or cursor < 0 or cursor > 2**62 or dt_ms != 0.1:
        raise ValueError("invalid native checkpoint clock")
    n = neuron_count
    slots = round(1.8 / dt_ms) + 1
    for name, array in arrays.items():
        if array.dtype.hasobject or not array.flags.c_contiguous:
            raise ValueError("unsafe checkpoint array representation")
        if array.dtype.kind == "f" and (not np.isfinite(array).all() or np.any(np.abs(array) > 1e12)):
            raise ValueError("nonfinite or unbounded checkpoint state")
    for name, shape, dtype in [
        ("queue", (slots, n), np.int32), ("queue_count", (slots,), np.int32),
        ("active", (n,), np.int32), ("active_flag", (n,), np.uint8),
        ("nactive", (1,), np.int32), ("refractory", (n,), np.int16),
        ("last", (n,), np.int64), ("eligibility_last", (n,), np.int64),
        ("modulation_last", (n,), np.int64),
    ]:
        value = arrays[name]
        if value.shape != shape or value.dtype != np.dtype(dtype):
            raise ValueError("native checkpoint layout mismatch")
    active_count = int(arrays["nactive"][0])
    if not 0 <= active_count <= n:
        raise ValueError("active list overflow")
    active = arrays["active"][:active_count]
    if np.any(active < 0) or np.any(active >= n) or len(np.unique(active)) != active_count:
        raise ValueError("invalid active neuron index")
    flags = np.zeros(n, dtype=np.uint8)
    flags[active] = 1
    if not np.array_equal(flags, arrays["active_flag"]):
        raise ValueError("active list and flags disagree")
    counts = arrays["queue_count"]
    if np.any(counts < 0) or np.any(counts > n) or counts[(cursor + slots - 1) % slots] != 0:
        raise ValueError("unsafe spike queue occupancy")
    for row, count in zip(arrays["queue"], counts):
        events = row[:int(count)]
        if np.any(events < 0) or np.any(events >= n) or len(np.unique(events)) != len(events):
            raise ValueError("invalid queued neuron index")
    if np.any(arrays["refractory"] < 0) or np.any(arrays["refractory"] > round(2.2 / dt_ms)):
        raise ValueError("invalid refractory clock")
    if np.any(arrays["last"] < -1) or np.any(arrays["last"] >= max(cursor, 1)):
        raise ValueError("invalid integration timestamp")
    for name in ["eligibility_last", "modulation_last"]:
        if np.any(arrays[name] < 0) or np.any(arrays[name] > cursor):
            raise ValueError("invalid trace timestamp")
    for name in ["counts", "eligibility", "modulation", "adaptation", "luminance", "r8_light"]:
        if name in arrays and np.any(arrays[name] < 0):
            raise ValueError("invalid negative neural state")
