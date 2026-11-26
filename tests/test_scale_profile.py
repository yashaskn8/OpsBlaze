"""Regression coverage for P1-15 SSE concurrency, P3-18 mixed lineage, and B4-06 overload/drain."""

import pytest

from backend.benchmarks.scale_profile import measure_mixed_lineage_and_overload, measure_sse_fanout


@pytest.mark.asyncio
async def test_controlled_sse_mixed_lineage_and_overload_profile(sessions_fixture):
    sse = await measure_sse_fanout(sessions_fixture, clients=16, cap_limit=32)
    assert sse["all_clients_matched"] is True
    assert sse["overflow_rejected"] is True
    assert sse["peak_memory_kib"] >= 0

    mixed = await measure_mixed_lineage_and_overload(
        sessions_fixture, evidence_count=60, concurrent_readers=4, tenant_cap=4,
    )
    assert mixed["mixed_lineage"]["summary"]["samples"] == 12
    assert mixed["mixed_lineage"]["page"]["samples"] == 12
    assert mixed["overload_and_drain"]["rejected_429"] == 4
    assert mixed["overload_and_drain"]["post_drain_admitted"] is True
