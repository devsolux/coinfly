#!/usr/bin/env python3
"""Offline async contention/Decimal benchmark; optional verified full controller.

No native vault, real credentials, exchange I/O or orders. Run from the checkout:
  .venv/bin/python scripts/benchmark_coinfly.py --operations 50000 --clients 64

A host integration may supply --controller-factory trusted_module:create_controller.
It MUST return a fresh FlyController constructed with verified assets and an
attested kernel. Without it, full-graph verification is explicitly NOT RUN.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import hashlib
import importlib
import json
import math
import os
import platform
import socket
import subprocess
import sys
import threading
import time
import tracemalloc
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from decimal import Inexact, getcontext, localcontext
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pydantic import Field, ValidationError

from coinfly.domain.context import ContextKey, DomainError, Environment, FrozenModel, ProductTrack
from coinfly.domain.decimal_math import ExactDecimal, exact_decimal, financial_context
from coinfly.security.credential_realms import CredentialMetadata, CredentialPurpose
from coinfly.security.keystore import NativeKeystore
from coinfly.security.signing_gateway import ES256SecretVerifier, SigningGateway
from coinfly.venues.coinbase.capability_handshake import KeyPermissions, PermissionEvidence
from coinfly.venues.coinbase.endpoint_profiles import endpoint_profile


class SyntheticVault:
    """Ephemeral test fixture, deliberately confined to this benchmark script."""

    def __init__(self):
        self.items = {}

    def check_identity(self):
        pass

    def put(self, binding_id, metadata_digest, secret):
        self.items[binding_id] = (metadata_digest, bytes(secret))

    def read(self, binding_id, metadata_digest):
        expected, secret = self.items[binding_id]
        if expected != metadata_digest:
            raise AssertionError("synthetic credential binding changed")
        return bytearray(secret)

    def delete(self, binding_id, metadata_digest):
        del self.items[binding_id]


class SyntheticAuthority:
    def __init__(self):
        self.lock = threading.RLock()
        self.allowed = True

    def require_authorized(self, request, *, now):
        if not self.allowed:
            raise DomainError("AUTHORITY_FENCED")

    def run_at_send_boundary(self, request, invoke):
        with self.lock:
            self.require_authorized(request, now=datetime.now(timezone.utc))
            return invoke()


def synthetic_gateway():
    context = ContextKey(authority_id="synthetic-benchmark", environment=Environment.LIVE,
                         product_track=ProductTrack.SPOT, broker_profile_id="synthetic-profile", epoch=1)
    metadata = CredentialMetadata(binding_id="a" * 32, environment=context.environment,
                                  verified_profile_id=context.broker_profile_id, authority_id=context.authority_id,
                                  portfolio_id="synthetic", purpose=CredentialPurpose.OBSERVATION, version=1)
    vault = SyntheticVault()
    keystore = NativeKeystore(vault)
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    secret = bytearray(json.dumps({"key_name": "organizations/synthetic/apiKeys/benchmark",
                                   "private_key": pem}).encode())
    credential = keystore.verify_staged(keystore.stage(metadata, secret), ES256SecretVerifier())
    now = datetime.now(timezone.utc)
    evidence = PermissionEvidence(binding_id=metadata.binding_id, credential_version=1,
        permissions=KeyPermissions(can_view=True, can_trade=False, can_transfer=False,
                                   portfolio_uuid="synthetic", portfolio_type="CONSUMER"),
        observed_at=now, expires_at=now + timedelta(hours=1), response_sha256="1" * 64)
    authority = SyntheticAuthority()
    return SigningGateway(context=context, endpoint=endpoint_profile("coinbase-advanced-spot"),
        credential=credential, profile_version=1, permissions=evidence, keystore=keystore,
        authority=authority), authority, vault


class MonetaryDTO(FrozenModel):
    quantity: ExactDecimal = Field(gt=0, allow_inf_nan=False, max_digits=38, decimal_places=18)
    price: ExactDecimal = Field(gt=0, allow_inf_nan=False, max_digits=38, decimal_places=18)


PAYLOAD = '{"quantity":"0.123456789012345678","price":"12345.123456789012345678"}'
# Independent integer coefficient product, with 36 fractional digits.
EXPECTED = D("1524.089301936160633746527968299765279684")


@exact_decimal
def decimal_work(iterations):
    for _ in range(iterations):
        item = MonetaryDTO.model_validate_json(PAYLOAD)
        with localcontext(financial_context(512)):
            result = item.quantity * item.price
        if result != EXPECTED:
            raise AssertionError("Decimal result drift")


class Samples:
    """Bounded recent-window quantiles plus whole-run maximum and count."""
    def __init__(self):
        self.values = deque(maxlen=20000)
        self.count = 0
        self.maximum = 0.

    def add(self, value):
        self.values.append(value)
        self.count += 1
        self.maximum = max(self.maximum, value)

    def report(self):
        values = sorted(self.values)
        return {"count": self.count, "quantile_window": len(values), "max_ms": self.maximum * 1000,
                **{f"p{n}_ms": values[min(len(values) - 1, math.ceil(len(values) * n / 100) - 1)] * 1000
                   if values else None for n in (50, 95, 99)}}


def rss_bytes():
    # Current RSS (not ru_maxrss), so retained memory is distinguishable from a peak.
    if sys.platform == "linux":
        pages = int(Path("/proc/self/statm").read_text().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE")
    if sys.platform == "darwin":
        result = subprocess.run(["/bin/ps", "-o", "rss=", "-p", str(os.getpid())],
                                check=True, capture_output=True, text=True, timeout=2)
        return int(result.stdout.strip()) * 1024
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        class MemoryCounters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        *[(name, ctypes.c_size_t) for name in ("PeakWorkingSetSize", "WorkingSetSize",
                          "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                          "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]]
        counters = MemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        kernel, psapi = ctypes.windll.kernel32, ctypes.windll.psapi
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(MemoryCounters), wintypes.DWORD]
        if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            raise OSError("process memory measurement failed")
        return counters.WorkingSetSize
    raise RuntimeError("current RSS measurement unsupported on this host")


def signing_work(gateway, submitted, decimal_iterations, transport_ms):
    started = time.perf_counter()
    ambient = getcontext()
    ambient.prec = 2
    ambient.traps[Inexact] = True
    decimal_start = time.perf_counter()
    decimal_work(decimal_iterations)
    decimal_elapsed = time.perf_counter() - decimal_start
    if ambient.prec != 2 or not ambient.traps[Inexact]:
        raise AssertionError("Decimal context escaped its scope")
    request = gateway.make_read_request(path="/accounts", query={})
    authorization = gateway.sign(request)
    def invoke():
        # Bounded synthetic request initiation; there is deliberately no socket.
        if transport_ms:
            time.sleep(transport_ms / 1000)
        return 1
    if gateway.send(request, authorization, invoke) != 1:
        raise AssertionError("synthetic invocation missing")
    return started - submitted, decimal_elapsed, threading.get_ident()


def graph_identity(controller):
    brain = controller.brain
    brain.paths.dataset.assert_current()
    if brain.n != 166700 or len(brain.post) != 25582938:
        raise AssertionError("full retained MaleCNS v1.0 graph required")
    return {name: hashlib.sha256(memoryview(getattr(brain, name))).hexdigest()
            for name in ("ids", "ptr", "post")}


def graph_work(controller, frame, stimulus):
    import numpy as np

    from coinfly.research.checkpoint import validate_native_state

    result = controller.observe(frame, stimulus)
    brain = controller.brain
    arrays = {name: getattr(brain, name) for name in brain.fields}
    validate_native_state(arrays, neuron_count=brain.n, cursor=brain.cursor)
    for name in ("weight", *brain.fields):
        array = getattr(brain, name)
        if array.dtype.kind == "f" and not np.isfinite(array).all():
            raise AssertionError(f"nonfinite full-graph state: {name}")
    if any(np.any(value < -.9) or np.any(value > 1) for value in (brain.memory_u, brain.memory_w)):
        raise AssertionError("plasticity efficacy bound violation")
    return result


async def stress(args, controller=None):
    initial_graph = graph_identity(controller) if controller is not None else None
    gateway, authority, vault = synthetic_gateway()
    try:
        MonetaryDTO.model_validate_json('{"quantity":0.1,"price":"1"}')
    except ValidationError:
        pass
    else:
        raise AssertionError("fractional JSON accepted")
    decimal_work(1)
    latencies, queue_waits, decimal_times, loop_lag, graph_times = (Samples() for _ in range(5))
    completions = [0] * args.clients
    thread_completions, errors = Counter(), Counter()
    memory = deque(maxlen=1200)
    done = asyncio.Event()
    loop = asyncio.get_running_loop()
    async def context_probe(index):
        with localcontext(financial_context(100 + index)):
            await asyncio.sleep(0)
            if getcontext().prec != 100 + index:
                raise AssertionError("async Decimal context cross-contamination")
    await asyncio.gather(*(context_probe(index) for index in range(args.clients)))
    gc.collect()
    tracemalloc.start()
    baseline_python = tracemalloc.get_traced_memory()[0]
    baseline_rss = rss_bytes()
    started = time.perf_counter()
    graph_steps = 0
    with ThreadPoolExecutor(max_workers=args.threads, thread_name_prefix="sign-decimal") as pool, \
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="full-graph") as graph_pool:
        # Warm every available thread before measuring retained memory trends.
        await asyncio.gather(*(loop.run_in_executor(pool, decimal_work, 10) for _ in range(args.threads)))

        async def client(index):
            for _ in range(index, args.operations, args.clients):
                sent = time.perf_counter()
                try:
                    queued, arithmetic, thread_id = await loop.run_in_executor(
                        pool, signing_work, gateway, sent, args.decimal_iterations, args.transport_ms)
                except DomainError as error:
                    errors[error.code] += 1
                    continue
                completions[index] += 1
                thread_completions[thread_id] += 1
                queue_waits.add(queued)
                decimal_times.add(arithmetic)
                # Simulate response waiting outside the signer/authority locks.
                if args.response_ms:
                    await asyncio.sleep(args.response_ms / 1000)
                latencies.add(time.perf_counter() - sent)

        async def monitor():
            last_memory = 0.
            while not done.is_set():
                expected = loop.time() + .01
                await asyncio.sleep(.01)
                loop_lag.add(max(0., loop.time() - expected))
                elapsed = time.perf_counter() - started
                if elapsed - last_memory >= 1:
                    memory.append({"elapsed_s": elapsed, "rss_bytes": rss_bytes(),
                                   "python_bytes": tracemalloc.get_traced_memory()[0],
                                   "completed": sum(completions)})
                    last_memory = elapsed

        async def graph():
            nonlocal graph_steps
            if controller is None:
                return
            import numpy as np
            rng = np.random.default_rng(20260920)
            while not done.is_set() or graph_steps < args.graph_steps:
                frame = rng.integers(0, 256, (64, 64, 3), dtype=np.uint8)
                tick = time.perf_counter()
                result = await loop.run_in_executor(graph_pool, graph_work, controller, frame,
                                                    ("none", "reward", "aversive")[graph_steps % 3])
                if result["brain_ms"] <= 0 or result["total_spikes"] < 0:
                    raise AssertionError("invalid full-graph output")
                graph_times.add(time.perf_counter() - tick)
                graph_steps += 1

        async def work():
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(monitor())
                tasks.create_task(graph())
                async def clients():
                    try:
                        await asyncio.gather(*(client(i) for i in range(args.clients)))
                    finally:
                        done.set()
                tasks.create_task(clients())
        await asyncio.wait_for(work(), timeout=args.timeout)
    elapsed = time.perf_counter() - started
    gc.collect()
    retained_python, peak_python = tracemalloc.get_traced_memory()
    retained_rss = rss_bytes()
    # Verify that a fence still blocks an already signed request under this load.
    fence_ok = None
    capacity_ok = None
    if args.operations < 100000:
        request = gateway.make_read_request(path="/accounts", query={})
        token = gateway.sign(request)
        with authority.lock:
            authority.allowed = False
        try:
            gateway.send(request, token, lambda: (_ for _ in ()).throw(AssertionError("fenced send")))
        except DomainError as error:
            fence_ok = error.code == "AUTHORITY_FENCED"
        else:
            fence_ok = False
    else:
        try:
            gateway.sign(gateway.make_read_request(path="/accounts", query={}))
        except DomainError as error:
            capacity_ok = error.code == "SIGNING_CAPACITY_EXCEEDED"
        else:
            capacity_ok = False
    gateway.retire()
    vault.items.clear()
    gc.collect()
    released_python = tracemalloc.get_traced_memory()[0]
    released_rss = rss_bytes()
    tracemalloc.stop()
    checks = {"all_operations_completed": sum(completions) == args.operations,
              "all_clients_progressed": all(completions), "all_executor_threads_progressed": len(thread_completions) == args.threads,
              "loop_lag_within_budget": loop_lag.maximum * 1000 <= args.max_loop_lag_ms,
              "queue_wait_within_budget": queue_waits.maximum * 1000 <= args.max_queue_wait_ms,
              "released_python_growth_within_budget": released_python - baseline_python <= args.max_retained_mib * 2**20,
              "released_rss_growth_within_budget": released_rss - baseline_rss <= args.max_rss_growth_mib * 2**20}
    if fence_ok is not None:
        checks["fence_preserved"] = fence_ok
    if capacity_ok is not None:
        checks["lifecycle_capacity_enforced"] = capacity_ok
    if args.require_full_graph:
        checks["full_graph_executed"] = controller is not None and graph_steps >= args.graph_steps
    if controller is not None:
        checks["full_graph_identity_preserved"] = graph_identity(controller) == initial_graph
    return {"status": "passed" if all(checks.values()) else "failed", "checks": checks,
            "mode": "synthetic-signing-and-exact-decimal", "network": "disabled", "configuration": vars(args),
            "runtime": {"python": sys.version, "platform": platform.platform(), "cpu_count": os.cpu_count()},
            "elapsed_s": elapsed, "completed": sum(completions), "operations_per_second": sum(completions) / elapsed,
            "latency": latencies.report(), "executor_queue": queue_waits.report(),
            "decimal_batch": decimal_times.report(), "event_loop_lag": loop_lag.report(),
            "client_completions": completions, "thread_completions": sorted(thread_completions.values()), "errors": dict(errors),
            "memory": {"baseline_python": baseline_python, "retained_python": retained_python, "peak_python": peak_python,
                       "released_python": released_python, "baseline_rss": baseline_rss, "retained_rss": retained_rss,
                       "released_rss": released_rss, "samples": list(memory),
                       "interpretation": "RSS minus traced allocations is NOT a fragmentation measurement; compare repeated warm runs."},
            "full_graph": {"status": "executed" if controller is not None else "not_run_missing_verified_assets",
                           "steps": graph_steps, "latency": graph_times.report(),
                           "neurons": controller.brain.n if controller else None,
                           "edges": len(controller.brain.post) if controller else None,
                           "graph_identity": initial_graph,
                           "kernel": controller.brain.build if controller else None,
                           "memory": controller.brain.memory() if controller else None},
            "post_load_fence_test": "passed" if fence_ok else "not_run_capacity_exhausted",
            "signer_lifecycle_capacity": 100000}


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operations", type=int, default=20000)
    parser.add_argument("--clients", type=int, default=32)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--decimal-iterations", type=int, default=20)
    parser.add_argument("--transport-ms", type=float, default=0.)
    parser.add_argument("--response-ms", type=float, default=0.)
    parser.add_argument("--timeout", type=float, default=300.)
    parser.add_argument("--max-loop-lag-ms", type=float, default=250.)
    parser.add_argument("--max-queue-wait-ms", type=float, default=2000.)
    parser.add_argument("--max-retained-mib", type=float, default=32.)
    parser.add_argument("--max-rss-growth-mib", type=float, default=128.)
    parser.add_argument("--controller-factory")
    parser.add_argument("--graph-steps", type=int, default=10)
    parser.add_argument("--require-full-graph", action="store_true")
    parser.add_argument("--rounds", type=int, default=1, help="Repeat after retiring each synthetic signer; compare warm RSS")
    parser.add_argument("--output", help="Write the JSON report to this local file")
    args = parser.parse_args(argv)
    if not (1 <= args.rounds <= 20 and 1 <= args.threads <= args.clients <= 256 and args.clients <= args.operations <= 100000
            and 1 <= args.decimal_iterations <= 10000 and 1 <= args.graph_steps <= 10000):
        parser.error("bounded positive workload required (threads <= clients <= 256; operations <= 100000)")
    for name in ("transport_ms", "response_ms", "timeout", "max_loop_lag_ms", "max_queue_wait_ms",
                 "max_retained_mib", "max_rss_growth_mib"):
        value = getattr(args, name)
        if not math.isfinite(value) or value < 0 or (name == "timeout" and value == 0):
            parser.error(f"invalid {name}")
    return args


def main():
    args = arguments()
    def deadline():
        # asyncio cancellation cannot stop a deadlocked native call or a worker
        # thread. This offline process owns no live state; bound it at process level.
        encoded = json.dumps({"status": "failed", "reason": "PROCESS_DEADLINE_EXCEEDED"})
        try:
            if args.output:
                Path(args.output).write_text(encoded + "\n")
            print(encoded, flush=True)
        finally:
            os._exit(2)
    watchdog = threading.Timer(args.timeout * args.rounds + 10, deadline)
    watchdog.daemon = True
    watchdog.start()
    def blocked(*_args, **_kwargs):
        raise RuntimeError("benchmark network access forbidden")
    with ExitStack() as stack:
        for name in ("connect", "connect_ex", "sendto"):
            stack.enter_context(patch.object(socket.socket, name, blocked))
        stack.enter_context(patch.object(socket, "getaddrinfo", blocked))
        controller = None
        if args.controller_factory:
            from coinfly.research.neural.controller import FlyController
            module, name = args.controller_factory.split(":", 1)
            controller = getattr(importlib.import_module(module), name)()
            if not isinstance(controller, FlyController):
                raise TypeError("verified FlyController required")
            controller.brain.paths.dataset.assert_current()
        reports = [asyncio.run(stress(args, controller)) for _ in range(args.rounds)]
        report = reports[0]
        if len(reports) > 1:
            warm_growth = reports[-1]["memory"]["released_rss"] - reports[0]["memory"]["released_rss"]
            passed = all(item["status"] == "passed" for item in reports)
            passed = passed and warm_growth <= args.max_rss_growth_mib * 2**20
            completed = sum(item["completed"] for item in reports)
            report = {"status": "passed" if passed else "failed", "rounds": reports,
                      "warm_released_rss_growth_bytes": warm_growth, "completed": completed,
                      "operations_per_second": completed / sum(item["elapsed_s"] for item in reports)}
        encoded = json.dumps(report, sort_keys=True, indent=2)
        if args.output:
            Path(args.output).write_text(encoded + "\n")
            print(json.dumps({"status": report["status"], "completed": report["completed"],
                              "operations_per_second": report["operations_per_second"], "output": args.output}))
        else:
            print(encoded)
    watchdog.cancel()
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
