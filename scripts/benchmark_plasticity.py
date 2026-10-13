#!/usr/bin/env python3
"""Synthetic long-sequence native plasticity ablation; no connectome/profit claim."""

import argparse
import json
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from coinfly.research.neural.experimental_plasticity import (
    EligibilityBank,
    ExperimentalPlasticity,
    PlasticityConfig,
    capture_kernel,
    plasticity_kernel,
)


def run(edges, bins, decay, saturation_decay):
    u, w = np.ones(edges), np.ones(edges)
    zero = np.zeros(edges)
    # Compile/load cache before timing. Reset state for a like-for-like ablation.
    capture_kernel(u, w, zero, zero, .01, .001, decay, saturation_decay, .95, .001)
    u.fill(1)
    w.fill(1)
    started = time.perf_counter()
    saturation_seconds = 0.
    peak_delta = 0.
    for _ in range(bins):
        saturation, _, _, delta, *_ = capture_kernel(u, w, zero, zero, .01, .001, decay, saturation_decay, .95, .001)
        saturation_seconds += saturation * .01
        peak_delta = max(peak_delta, delta)
    elapsed = time.perf_counter() - started
    if not np.isfinite(u).all() or not np.isfinite(w).all() or np.any(w < -.9) or np.any(w > 1):
        raise AssertionError("nonfinite or out-of-bounds plasticity")
    return {"elapsed_seconds": elapsed, "edge_bin_updates_per_second": edges * bins / elapsed,
            "simulated_neural_seconds": bins * .01, "final_mean_u": float(u.mean()),
            "final_mean_w": float(w.mean()), "near_saturation_time_fraction": saturation_seconds / (bins * .01),
            "maximum_unbounded_delta": peak_delta, "state_array_bytes": u.nbytes + w.nbytes + zero.nbytes}


def run_ring(edges, decisions):
    config = replace(PlasticityConfig(), credit_tau_seconds=.5, maximum_credit_age_seconds=1.)
    bank = EligibilityBank(edges, config)
    start = datetime(2026, 9, 20, tzinfo=timezone.utc)
    tag = np.full(edges, .1)
    bank.purge_expired(start)  # exclude JIT/cache initialization
    storage = bank._values
    began = time.perf_counter()
    admitted = maximum_live = 0
    for index in range(decisions):
        now = start + timedelta(milliseconds=index)
        if bank.reserve(now):
            bank.store(now, tag)
            admitted += 1
        maximum_live = max(maximum_live, len(bank))
        assert bank._values is storage and len(bank) <= bank.capacity
    assert admitted + bank.capacity_rejections == decisions
    assert admitted == bank.expired_tags + len(bank)
    return {"elapsed_seconds": time.perf_counter() - began, "decisions": decisions,
            "cadence_hz": 1000, "credit_age_seconds": 1, "capacity": bank.capacity,
            "admitted": admitted, "rejected_for_capacity": bank.capacity_rejections,
            "expired": bank.expired_tags, "maximum_live": maximum_live,
            "tag_array_bytes": storage.nbytes, "storage_reused": True}


def run_pipeline(edges, bins):
    """Synthetic rate bins with a fixed captured tag; excludes neural propagation."""
    brain = SimpleNamespace(
        circuit={"edges": np.arange(edges), "pre": np.arange(edges, dtype=np.int32),
                 "dan": np.array([edges + 1], dtype=np.int32), "gain": np.ones((1, edges), dtype=np.float32)},
        post=np.full(edges, edges, dtype=np.int32), weight=np.ones(edges, dtype=np.float32),
        baseline_plastic=np.ones(edges, dtype=np.float32), dan_baseline_hz=np.zeros(1),
        memory_u=np.zeros(edges), memory_w=np.zeros(edges), eta=.001,
        weights_frozen=False, plasticity_diagnostics={})
    engine = ExperimentalPlasticity(brain, PlasticityConfig())
    counts = np.ones(edges + 2, dtype=np.int32)
    stimulation = (brain.circuit["dan"], 20.)
    engine.advance(brain, counts, .01, learning=True, stimulation=stimulation, delta_limit=.001)
    engine.pre_trace.fill(0)
    engine.post_trace.fill(0)
    engine.tag.fill(0)
    engine.credit.fill(.25)  # explicitly synthetic delayed-credit fixture
    brain.plasticity_diagnostics.clear()
    storage = engine.credit
    before_bytes = engine.buffer_nbytes
    began = time.perf_counter()
    for index in range(bins):
        counts[edges] = index % 2
        engine.advance(brain, counts, .01, learning=True, stimulation=stimulation, delta_limit=.001)
    elapsed = time.perf_counter() - began
    bounded = all(np.isfinite(a).all() and np.all(a >= -.9) and np.all(a <= 1)
                  for a in (brain.memory_u, brain.memory_w))
    mapped = np.array_equal(brain.weight, (brain.baseline_plastic * (1 + brain.memory_w)).astype(np.float32))
    return {"elapsed_seconds": elapsed, "edge_bin_updates_per_second": edges * bins / elapsed,
            "mean_bin_microseconds": elapsed / bins * 1e6, "engine_array_bytes": engine.buffer_nbytes,
            "storage_reused": storage is engine.credit and engine.buffer_nbytes == before_bytes,
            "bounded_states": bool(bounded), "weights_match_efficacy": bool(mapped),
            "capture_active_bins": brain.plasticity_diagnostics["capture_active_bins"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--edges", type=int, default=4096)
    parser.add_argument("--bins", type=int, default=20000)
    parser.add_argument("--decisions", type=int, default=20000)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if (min(args.edges, args.bins, args.decisions) < 1 or args.edges * args.bins > 1000000000
            or max(args.edges, args.decisions) > 1000000):
        parser.error("positive bounded edge/bin work budget required")
    disabled = run(args.edges, args.bins, 0., 0.)
    enabled = run(args.edges, args.bins, .05, .5)
    ring = run_ring(args.edges, args.decisions)
    pipeline = run_pipeline(args.edges, args.bins)
    checks = {"disabled_control_stays_saturated": disabled["final_mean_w"] == 1,
              "homeostasis_recovers_headroom": enabled["final_mean_w"] < 1,
              "nopython_kernel": bool(capture_kernel.nopython_signatures), "bounded_tag_ring": ring["storage_reused"],
              "nopython_pipeline": bool(plasticity_kernel.nopython_signatures),
              "bounded_pipeline": pipeline["bounded_states"] and pipeline["weights_match_efficacy"],
              "reused_pipeline_storage": pipeline["storage_reused"]}
    report = {"scope": "synthetic plasticity ablation only", "full_graph": "not_run",
              "edges": args.edges, "bins": args.bins, "disabled": disabled, "enabled": enabled, "tag_ring": ring,
              "pipeline": pipeline, "checks": checks, "passed": all(checks.values())}
    payload = json.dumps(report, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n")
    print(payload)
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
