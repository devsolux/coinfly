import asyncio

from scripts import benchmark_coinfly as benchmark


def test_offline_async_harness_preserves_decimal_and_signing_invariants(monkeypatch):
    # Resource values are synthetic in this regression. Native measurements are
    # exercised by the separately run benchmark, never inferred from this test.
    monkeypatch.setattr(benchmark, "rss_bytes", lambda: 10_000_000)
    args = benchmark.arguments(["--operations", "128", "--clients", "8", "--threads", "2",
                                "--decimal-iterations", "2"])
    report = asyncio.run(benchmark.stress(args))
    assert report["status"] == "passed"
    assert report["completed"] == 128
    assert report["full_graph"]["status"] == "not_run_missing_verified_assets"
    assert report["errors"] == {}


def test_required_full_graph_cannot_silently_pass_without_assets(monkeypatch):
    monkeypatch.setattr(benchmark, "rss_bytes", lambda: 10_000_000)
    args = benchmark.arguments(["--operations", "8", "--clients", "1", "--threads", "1",
                                "--require-full-graph"])
    report = asyncio.run(benchmark.stress(args))
    assert report["status"] == "failed"
    assert report["checks"]["full_graph_executed"] is False
