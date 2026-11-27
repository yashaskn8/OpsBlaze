
import pytest
from sqlalchemy import event, select

from backend.experiments.replay import (
    FIXTURES, FutureEvidenceDenied, ProbeBudgetExceeded,
    Twin, create_pair, list_pairs_page, pair_report,
    resume_pair, run_arm, run_paired_trials, start_pair,
)
from backend.models import ReplayArm, ReplayExperiment


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


@pytest.mark.asyncio
@pytest.mark.parametrize(("field", "value", "reason"), [
    ("budget", 5, "FIXTURE_DIGEST_MISMATCH"),
    ("workload_version", "unknown", "FIXTURE_DIGEST_MISMATCH"),
    ("evidence_policy_version", "unknown", "FIXTURE_DIGEST_MISMATCH"),
    ("namespace", "other-run/runbook", "STORED_ARM_MISMATCH"),
    ("probe_cost", 0, "STORED_ARM_MISMATCH"),
    ("oracle_passed", False, "STORED_ARM_MISMATCH"),
    ("trace", [], "STORED_ARM_MISMATCH"),
])
async def test_resume_rejects_inconsistent_stored_experiment(sessions_fixture, field, value, reason):
    async with sessions_fixture() as session:
        experiment = await start_pair(session, "tenant", "recoverable-v1", seed=42)
    async with sessions_fixture.begin() as session:
        if reason == "FIXTURE_DIGEST_MISMATCH":
            row = await session.get(ReplayExperiment, experiment.id)
        else:
            row = await session.scalar(select(ReplayArm).where(ReplayArm.experiment_id == experiment.id))
        setattr(row, field, value)
    async with sessions_fixture() as session:
        result = await resume_pair(session, "tenant", experiment.id)
        assert result.status == "INVALIDATED"
        assert result.invalidation_reason == reason
        report = await pair_report(session, "tenant", experiment.id)
        assert [arm["kind"] for arm in report["arms"]] == ["RUNBOOK"]


@pytest.mark.asyncio
async def test_pair_report_reads_state_and_arms_in_one_query(sessions_fixture):
    await assert_replay_report_snapshot(sessions_fixture)


@pytest.mark.integration
@pytest.mark.pg_e2e
@pytest.mark.asyncio
async def test_native_replay_report_reads_state_and_arms_in_one_query(native_sessions_fixture):
    await assert_replay_report_snapshot(native_sessions_fixture)


async def assert_replay_report_snapshot(sessions):
    async with sessions() as session:
        experiment = await start_pair(session, "tenant", "recoverable-v1", seed=42, initial_arms=0)

    reads = []

    def count_read(_conn, _cursor, statement, _parameters, _context, _executemany):
        reads.append(statement)

    engine = sessions.kw["bind"].sync_engine
    event.listen(engine, "before_cursor_execute", count_read)
    try:
        async with sessions() as session:
            report = await pair_report(session, "tenant", experiment.id)
        assert report["status"] == "PENDING" and report["arms"] == []
        assert len(reads) == 1
    finally:
        event.remove(engine, "before_cursor_execute", count_read)


@pytest.mark.asyncio
async def test_resumable_pair_invalidation_and_distinct_arm_trials(sessions_fixture):
    from httpx import ASGITransport, AsyncClient
    from backend.api import app
    from backend.database import get_session
    from backend.settings import Settings, get_settings

    # Partial pair starts with 1 arm (RUNBOOK) and resumes to COMPLETE.
    async with sessions_fixture() as session:
        partial = await start_pair(session, "t-s1", "already-healthy-v1", seed=9, initial_arms=1)
        assert partial.status == "RUNNING"
    async with sessions_fixture() as session:
        done = await resume_pair(session, "t-s1", partial.id)
        assert done.status == "COMPLETE"
        report = await pair_report(session, "t-s1", partial.id)
        by_kind = {a["kind"]: a for a in report["arms"]}
        assert by_kind["RUNBOOK"]["probe_cost"] == 2
        assert by_kind["ADAPTIVE_BASELINE"]["probe_cost"] == 1
        assert by_kind["ADAPTIVE_BASELINE"]["outcome"] == "ABSTAINED"

    # Leaking / premature pair is marked INVALIDATED while retaining evidence.
    async with sessions_fixture() as session:
        leaking = await start_pair(session, "t-s1", "recoverable-v1", seed=11, initial_arms=1)
    async with sessions_fixture() as session:
        invalidated = await resume_pair(session, "t-s1", leaking.id, inject_leak=True)
        assert invalidated.status == "INVALIDATED"
        assert invalidated.invalidation_reason == "FUTURE_EVIDENCE_OR_CONTAMINATION"
        inv_report = await pair_report(session, "t-s1", leaking.id)
        assert len(inv_report["arms"]) == 2
        assert any(a["outcome"] == "INVALIDATED" for a in inv_report["arms"])

    async with sessions_fixture() as session:
        summary = await run_paired_trials(session, "t-s1", "already-healthy-v1", [1, 2, 3])
        assert summary["trials"] == 3
        assert summary["adaptive_probe_saved"] == 3
        assert summary["measured_ai_comparison"] is False
        rows, _ = await list_pairs_page(session, "t-s1", limit=10)
        assert len(rows) == 5

    async def override_session():
        async with sessions_fixture() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: Settings(local_token="s1-token")
    try:
        headers = {"X-Tenant-Id": "t-s1", "X-OpsBlaze-Token": "s1-token"}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            listing = await client.get("/v1/experiments?limit=2", headers=headers)
            assert listing.status_code == 200 and len(listing.json()["records"]) == 2
            resumed = await client.post(f"/v1/experiments/{partial.id}/resume", headers=headers)
            assert resumed.status_code == 202 and resumed.json()["status"] == "COMPLETE"
    finally:
        app.dependency_overrides.clear()
