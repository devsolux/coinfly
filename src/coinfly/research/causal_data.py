"""Polars feature-evidence audit before Decimal vectorization and RGB rendering."""

import hashlib
import re
from datetime import datetime
from typing import Literal, cast

import numpy as np
import polars as pl

from coinfly.research.frames import AuditedFrame
from coinfly.research.reward import ObservationEvidence
from coinfly.research.vectorization import TickScale, vectorize

__all__ = ["AuditedFrame", "CausalFrameLoader", "TIME_COLUMNS"]

TIME_COLUMNS = ("event_at", "available_at", "window_end", "fitted_through")


class CausalFrameLoader:
    """Strict registered features and per-row lineage, no permissive drop/filter.

    Input is one long-form feature row per (event_at, feature). Future rows are
    rejected rather than silently filtered. Provenance is supplied by the trusted
    feature builder; timestamps cannot prove that dishonestly labeled values are causal.
    """

    def __init__(self, scales: tuple[TickScale, ...], *, max_rows: int = 100000) -> None:
        names = [spec.name for spec in scales]
        if (not names or len(set(names)) != len(names) or max_rows < 1
                or any(re.search(r"future|forward|lead|target|label|fill|pnl", name, re.I) for name in names)):
            raise ValueError("unique market-only feature names required")
        self.scales = scales
        self.max_rows = max_rows

    def frame(self, table: pl.DataFrame, *, decision_at: datetime, partition: Literal["train", "evaluation"],
              evaluation_start: datetime | None = None) -> AuditedFrame:
        if decision_at.tzinfo is None:
            raise ValueError("aware decision time required")
        expected = {"feature", "value", "source_kind", *TIME_COLUMNS}
        if set(table.columns) != expected or not 0 < table.height <= self.max_rows:
            raise ValueError("feature schema/row budget mismatch")
        for name in TIME_COLUMNS:
            dtype = table.schema[name]
            if not isinstance(dtype, pl.Datetime) or dtype.time_zone != "UTC":
                raise ValueError("feature lineage must use typed UTC timestamps")
        if any(table.null_count().row(0)):
            raise ValueError("null feature evidence")
        names = [spec.name for spec in self.scales]
        if set(table["feature"].unique().to_list()) != set(names):
            raise ValueError("unregistered or missing feature")
        invalid = table.filter(
            (pl.col("source_kind") != "market")
            | (pl.col("event_at") > pl.col("window_end"))
            | (pl.col("window_end") > pl.col("available_at"))
            | (pl.col("available_at") > pl.lit(decision_at))
            | (pl.col("fitted_through") > pl.lit(decision_at)))
        if invalid.height:
            raise ValueError("forward-looking, unavailable, or execution-report feature")
        scale_lineage = [spec.fitted_through for spec in self.scales if spec.fitted_through is not None]
        if any(fitted > decision_at for fitted in scale_lineage):
            raise ValueError("tick scale fitted after the decision instant")
        if partition == "evaluation":
            if evaluation_start is None or evaluation_start.tzinfo is None or evaluation_start > decision_at:
                raise ValueError("explicit holdout boundary required")
            if table.filter(pl.col("fitted_through") >= pl.lit(evaluation_start)).height:
                raise ValueError("preprocessing fitted on holdout")
            if any(fitted >= evaluation_start for fitted in scale_lineage):
                raise ValueError("tick scale fitted on holdout")
        if table.select(pl.struct("event_at", "feature").n_unique()).item() != table.height:
            raise ValueError("duplicate feature rows")
        if not isinstance(table.schema["value"], pl.Decimal):
            raise ValueError("feature values must be exact Polars Decimal, not binary floats")
        wide = table.pivot(on="feature", index="event_at", values="value").sort("event_at")
        selected = wide.select(names)
        if any(selected.null_count().row(0)):
            raise ValueError("ragged feature frame")
        tensor = vectorize(selected.rows(), self.scales)
        # Declared sensory projection only. The original normalized float tensor
        # and exact ticks remain available; RGB is deliberately an 8-bit display.
        pixels = np.rint((np.clip(tensor.values, -1, 1) + 1) * 127.5).astype(np.uint8)
        rgb = np.repeat(pixels[:, :, None], 3, axis=2)
        rgb.flags.writeable = False
        fitted_through = max((cast(datetime, table["fitted_through"].max()), *scale_lineage))
        evidence = ObservationEvidence(decision_at=decision_at, features_event_through=cast(datetime, table["window_end"].max()),
            features_available_through=cast(datetime, table["available_at"].max()), preprocessing_fitted_through=fitted_through,
            partition=partition, evaluation_start=evaluation_start)
        return AuditedFrame(rgb, tensor, evidence, hashlib.sha256(rgb.tobytes()).hexdigest())
