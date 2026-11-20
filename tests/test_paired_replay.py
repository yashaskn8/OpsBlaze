
import pytest

from backend.experiments.replay import (
    FIXTURES, FutureEvidenceDenied, ProbeBudgetExceeded,
    Twin, create_pair, pair_report, run_arm,
)


def test_future_evidence_denied_before_release():
    twin = Twin(FIXTURES["recoverable-v1"], seed=5)
    with pytest.raises(FutureEvidenceDenied):
        twin.probe("version")
    assert twin.calls == 0
    assert twin.trace[0]["outcome"] == "FUTURE_DENIED"


def test_probe_budget_enforced_before_side_effect():
    twin = Twin(FIXTURES["recoverable-v1"], seed=7, budget=1)
    twin.probe("health")
    twin.advance(2)
    with pytest.raises(ProbeBudgetExceeded):
        twin.probe("version")
    assert twin.calls == 1


def test_twin_state_isolation_and_false_ack_oracle():
    fixture = FIXTURES["recoverable-v1"]
    first = run_arm(fixture, seed=7, kind="RUNBOOK")
    second = Twin(fixture, seed=7)
    assert first.healthy is True
    assert second.healthy is False

    lying_adapter = run_arm(FIXTURES["false-ack-v1"], 7, "ADAPTIVE_BASELINE")
    assert lying_adapter.trace[-1]["outcome"] == "SUCCESS_ACK"
    assert lying_adapter.independent_oracle() is False


@pytest.mark.asyncio
async def test_pair_is_reproducible_and_tenant_scoped(sessions_fixture):
    async with sessions_fixture() as session:
        experiment = await create_pair(session, "tenant", "false-ack-v1", seed=42)
    async with sessions_fixture() as session:
        result = await pair_report(session, "tenant", experiment.id)
        assert result["measured_ai_comparison"] is False
        assert result["status"] == "COMPLETE"
        assert {arm["kind"] for arm in result["arms"]} == {"RUNBOOK", "ADAPTIVE_BASELINE"}
        assert all(arm["outcome"] == "FAILED" for arm in result["arms"])
        assert result["arms"][0]["namespace"] != result["arms"][1]["namespace"]
        assert await pair_report(session, "other", experiment.id) is None
