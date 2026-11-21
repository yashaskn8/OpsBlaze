"""B4 deterministic in-process capacity checks, not an external load test."""

import pytest

from backend.benchmarks.local_capacity import nearest_rank_ms, run_local_profile


def test_nearest_rank_math_is_exact_for_twelve_samples():
    values = [float(i) for i in range(12, 0, -1)]
    assert nearest_rank_ms(values, 0.50) == 6
    assert nearest_rank_ms(values, 0.95) == 12
    assert nearest_rank_ms(values, 0.99) == 12
    for values, percentile in (([], 0.95), ([1.0], 0), ([1.0], 1.1)):
        with pytest.raises(ValueError):
            nearest_rank_ms(values, percentile)


@pytest.mark.asyncio
async def test_bounded_capacity_profile_never_uses_network_or_production_labels():
    report = await run_local_profile()
    assert report["production_slo_certified"] is False
    assert report["network_requests"] == 0
    assert report["scope"] == "in-process SQLite/ASGI only"
    assert report["admissions"] == 12
    assert report["read_concurrency_steps"] == [1, 2, 4]
    assert set(report["measurements"]) == {
        "admission", "snapshot", "notifications",
    }
    for kind, metrics in report["measurements"].items():
        assert metrics["samples"] == (12 if kind == "admission" else 36)
        assert 0 <= metrics["p50_ms"] <= metrics["p95_ms"] <= metrics["p99_ms"]
        assert len(report["raw_latency_ms"][kind]) == metrics["samples"]
    assert "secret" not in str(report).lower()
