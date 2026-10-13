#!/usr/bin/env python3
"""Synthetic async data/JIT benchmark. No graph or profitability claim."""

import argparse
import asyncio
import gc
import json
import os
import platform
import threading
import time
import tracemalloc
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from decimal import Inexact, localcontext
from pathlib import Path

# Bound nested native pools before importing Polars. Respect host configuration.
os.environ.setdefault("POLARS_MAX_THREADS", "2")

import numpy as np  # noqa: E402
import polars as pl  # noqa: E402
from benchmark_coinfly import rss_bytes  # noqa: E402

from coinfly.research.causal_data import CausalFrameLoader  # noqa: E402
from coinfly.research.reward import RewardWindow  # noqa: E402
from coinfly.research.risk_reward import RiskAdjustedReward, latency_residual, risk_signal  # noqa: E402
from coinfly.research.vectorization import TickScale, _normalize_ticks_native  # noqa: E402


def fixture(rows, features):
    now = datetime(2026, 9, 20, tzinfo=timezone.utc)
    names = [f"market_{i}" for i in range(features)]
    times = [now + timedelta(microseconds=i) for i in range(rows) for _ in names]
    table = pl.DataFrame({"feature": names * rows, "value": [D("100.01")] * (rows * features),
                          "source_kind": ["market"] * (rows * features), "event_at": times,
                          "window_end": times, "available_at": times, "fitted_through": [now] * len(times)})
    loader = CausalFrameLoader(tuple(TickScale(name, D(100), D("0.01"), 100) for name in names))
    decision = times[-1] + timedelta(seconds=1)
    reward = RewardWindow(start_at=now, end_at=now + timedelta(seconds=1), available_at=decision,
                          currency="USD", pnl=D("-0.01"), capital=D(1000), basis="net_execution")
    return loader, table, decision, reward


def batch(loader, table, decision, reward):
    start = time.perf_counter()
    with localcontext() as ambient:
        ambient.prec = 2
        ambient.traps[Inexact] = True
        result = loader.frame(table, decision_at=decision, partition="train")
        shaped = RiskAdjustedReward()(reward)
        if (result.tensor.exact_value(0, 0) != D("100.01") or shaped.net_pnl != D("-0.01")
                or ambient.prec != 2 or not ambient.traps[Inexact]):
            raise AssertionError("exact conversion/accounting/context isolation failure")
    return time.perf_counter() - start


async def benchmark(args):
    fixture_args = fixture(args.rows, args.features)
    cold_start = time.perf_counter()
    batch(*fixture_args)
    cold_seconds = time.perf_counter() - cold_start
    # Separate compilation/cache loading from the measured warm workload.
    batch(*fixture_args)
    tracemalloc.start()
    reports = []
    loop = asyncio.get_running_loop()
    with ThreadPoolExecutor(max_workers=args.workers, thread_name_prefix="training-benchmark") as pool:
        for _ in range(args.rounds):
            latencies, lags = deque(maxlen=10000), deque(maxlen=10000)
            counts = [0] * args.workers
            done = asyncio.Event()
            started = time.perf_counter()
            async def client(index):
                for _ in range(index, args.batches, args.workers):
                    elapsed = await loop.run_in_executor(pool, batch, *fixture_args)
                    latencies.append(elapsed)
                    counts[index] += 1
            async def heartbeat():
                while not done.is_set():
                    expected = loop.time() + .01
                    await asyncio.sleep(.01)
                    lags.append(max(0, loop.time() - expected))
            monitor = asyncio.create_task(heartbeat())
            try:
                await asyncio.wait_for(asyncio.gather(*(client(i) for i in range(args.workers))), args.timeout)
            finally:
                done.set()
                await monitor
            seconds = time.perf_counter() - started
            gc.collect()
            reports.append({"seconds": seconds, "batches": sum(counts),
                            "exact_values_per_second": args.batches * args.rows * args.features / seconds,
                            "p50_ms": float(np.quantile(latencies, .5) * 1000),
                            "p99_ms": float(np.quantile(latencies, .99) * 1000),
                            "maximum_loop_lag_ms": max(lags, default=0) * 1000, "client_progress": counts,
                            "rss_bytes": rss_bytes(), "python_live_bytes": tracemalloc.get_traced_memory()[0]})
    tracemalloc.stop()
    growth = reports[-1]["rss_bytes"] - reports[0]["rss_bytes"]
    checks = {"every_client_progressed": all(min(r["client_progress"]) > 0 for r in reports),
              "bounded_loop_lag": all(r["maximum_loop_lag_ms"] <= args.max_loop_lag_ms for r in reports),
              "bounded_warm_rss_retention": growth <= args.max_rss_growth_mb * 1024**2,
              "nopython_jit_active": all(bool(kernel.nopython_signatures)
                                        for kernel in (_normalize_ticks_native, latency_residual, risk_signal))}
    return {"workload": "synthetic causal Decimal/Polars/JIT only", "full_graph": "not_run",
            "python": platform.python_version(), "machine": platform.machine(),
            "shape": [args.rows, args.features], "polars_threads": pl.thread_pool_size(),
            "cold_or_cache_load_seconds": cold_seconds, "rounds": reports,
            "warm_rss_growth_bytes": growth, "checks": checks, "passed": all(checks.values())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=128)
    parser.add_argument("--features", type=int, default=8)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--batches", type=int, default=200)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--max-loop-lag-ms", type=float, default=1000)
    parser.add_argument("--max-rss-growth-mb", type=float, default=128)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if (not 1 <= args.rows * args.features <= 100000 or min(args.rows, args.features) < 1
            or not 1 <= args.workers <= 64 or not args.workers <= args.batches <= 100000
            or not 2 <= args.rounds <= 20 or not 0 < args.timeout <= 3600
            or not 0 < args.max_loop_lag_ms <= 60000 or not 0 < args.max_rss_growth_mb <= 4096):
        parser.error("invalid or unbounded benchmark budget")
    # asyncio cancellation cannot terminate a stuck native worker thread.
    watchdog = threading.Timer(args.timeout * args.rounds + 60, lambda: os._exit(124))
    watchdog.daemon = True
    watchdog.start()
    try:
        report = asyncio.run(benchmark(args))
    finally:
        watchdog.cancel()
    payload = json.dumps(report, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n")
    print(payload)
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
