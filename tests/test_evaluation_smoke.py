import asyncio
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select, text, update

from backend.api import app
from backend.database import get_engine, get_session_maker
from backend.evaluation.fixtures import (
    FIXTURES, candidate_config, evaluate_strategy, oracle_expected,
)
from backend.evaluation.smoke import EvaluationConflict, freeze, report, run_smoke
from backend.evaluation.stats import paired_smoke_statistics
from backend.models import (
    EvaluationManifest, EvaluationTrial, FailureCluster, ImprovementCandidate,
)
from backend.settings import get_settings


async def seed_candidates(sessions, tenant_id):
    cluster_id = uuid4()
    async with sessions() as session:
        async with session.begin():
            session.add(
                FailureCluster(
                    id=cluster_id, tenant_id=tenant_id, signature="EFFECT_FAILED:fixture",
                    version=1, case_count=1, quarantined=False,
                )
            )
            good_id = uuid4()
            bad_id = uuid4()
            for candidate_id, spec in (
                (good_id, {"max_context_tokens": 512}),
                (bad_id, {"max_context_tokens": 64}),
            ):
                session.add(
                    ImprovementCandidate(
                        id=candidate_id, tenant_id=tenant_id, source_cluster_id=cluster_id,
                        kind="CONTEXT_LIMIT", base_policy_id="deterministic-v1",
                        change_spec=spec, hypothesis="bounded probe context",
                        risk="LOW", evaluation_manifest_id="fixed-smoke-v1",
                        state="EVALUATION_READY", version=2,
                        created_by="local_operator",
                    )
                )
    return good_id, bad_id


@pytest.fixture
async def candidate_lab(sessions_fixture):
    good_id, bad_id = await seed_candidates(sessions_fixture, "t")
    return sessions_fixture, good_id, bad_id


@pytest.mark.integration
@pytest.mark.pg_e2e
@pytest.mark.asyncio
async def test_parallel_postgres_evaluation_registration_returns_one_frozen_manifest():
    settings = get_settings()
    assert settings.database_url.startswith("postgresql+asyncpg:")
    engine = get_engine()
    sessions = get_session_maker()
    tenant = f"eval-race-{uuid4().hex}"
    requests = []
    try:
        candidate_id, _ = await seed_candidates(sessions, tenant)
        headers = {"X-Tenant-Id": tenant, "X-OpsBlaze-Token": settings.local_token}
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as client:
            # Allow both transactions to read before either can insert.
            async with engine.begin() as blocker:
                await blocker.execute(text("LOCK TABLE evaluation_manifest IN SHARE MODE"))
                requests = [asyncio.create_task(client.post(
                    "/v1/evaluations", headers=headers, json={"candidate_id": str(candidate_id)},
                )) for _ in range(2)]
                pending_inserts = text("""
                    SELECT count(*) FROM pg_locks
                    WHERE relation = 'evaluation_manifest'::regclass
                      AND mode = 'RowExclusiveLock' AND NOT granted
                """)
                async with asyncio.timeout(10):
                    while await blocker.scalar(pending_inserts) < 2:
                        await asyncio.sleep(0.01)
            results = await asyncio.wait_for(asyncio.gather(*requests), 10)

        assert [result.status_code for result in results] == [201, 201]
        manifest = results[0].json()
        assert results[1].json() == manifest
        assert manifest["state"] == "FROZEN"
        async with sessions() as session:
            assert await session.scalar(select(func.count()).select_from(EvaluationManifest).where(
                EvaluationManifest.tenant_id == tenant,
            )) == 1
    finally:
        for request in requests:
            request.cancel()
        await asyncio.gather(*requests, return_exceptions=True)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.pg_e2e
async def test_postgres_registration_waits_for_candidate_invalidation():
    settings = get_settings()
    assert settings.database_url.startswith("postgresql+asyncpg:")
    engine = get_engine()
    sessions = get_session_maker()
    tenant = f"eval-invalidation-{uuid4().hex}"
    request = None
    try:
        candidate_id, _ = await seed_candidates(sessions, tenant)
        headers = {"X-Tenant-Id": tenant, "X-OpsBlaze-Token": settings.local_token}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            async with engine.begin() as invalidation:
                await invalidation.execute(update(ImprovementCandidate).where(
                    ImprovementCandidate.tenant_id == tenant, ImprovementCandidate.id == candidate_id,
                ).values(state="INVALID", version=3))
                request = asyncio.create_task(client.post(
                    "/v1/evaluations", headers=headers, json={"candidate_id": str(candidate_id)},
                ))
                # Let the request reach the scoped candidate read before committing invalidation.
                waiting = text("""
                    SELECT EXISTS (
                        SELECT 1 FROM pg_locks
                        WHERE locktype = 'transactionid' AND NOT granted
                          AND transactionid = pg_current_xact_id()::xid
                    )
                """)
                async with asyncio.timeout(10):
                    while not request.done() and not await invalidation.scalar(waiting):
                        await asyncio.sleep(0.01)
            result = await asyncio.wait_for(request, 10)
        assert result.status_code == 409, result.text
        async with sessions() as session:
            assert await session.scalar(select(EvaluationManifest.id).where(
                EvaluationManifest.tenant_id == tenant,
            )) is None
    finally:
        if request is not None:
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)
        await engine.dispose()


@pytest.mark.asyncio
async def test_frozen_manifest_idempotent_and_good_smoke(candidate_lab):
    sessions, candidate_id, _ = candidate_lab
    async with sessions() as session:
        first = await freeze(session, "t", candidate_id)
    async with sessions() as session:
        second = await freeze(session, "t", candidate_id)
        assert second.id == first.id
    async with sessions() as session:
        completed = await run_smoke(session, "t", first.id)
        assert completed.state == "SMOKE_PASS"
    async with sessions() as session:
        again = await run_smoke(session, "t", first.id)
        assert again.state == "SMOKE_PASS"
        data = await report(session, "t", first.id)
        assert len(data["trials"]) == 2 * len(FIXTURES)
        assert all(gate["passed"] for gate in data["gates"])
        assert data["integrity"] == "COMPLETE"
        assert data["statistics"]["pairs"] == len(FIXTURES)
        assert data["statistics"]["candidate_wins"] == 0
        assert data["statistics"]["verified_recovery_per_cost"] is None
        assert data["statistics"]["conservative_95pct_interval"] == [-1.0, 1.0]
        assert data["promotion_eligible"] is False
        assert await report(session, "another-tenant", first.id) is None


@pytest.mark.asyncio
async def test_changed_payload_at_same_version_preserves_the_frozen_manifest(candidate_lab):
    sessions, candidate_id, _ = candidate_lab
    async with sessions() as session:
        original = await freeze(session, "t", candidate_id)

    async with sessions() as session:
        async with session.begin():
            candidate = await session.get(ImprovementCandidate, candidate_id)
            candidate.change_spec = {"max_context_tokens": 64}

    async with sessions() as session:
        with pytest.raises(EvaluationConflict, match="candidate version"):
            await freeze(session, "t", candidate_id)
        frozen = (await session.scalars(select(EvaluationManifest))).one()
        assert frozen.id == original.id
        assert frozen.candidate_digest == original.candidate_digest
        assert frozen.state == "FROZEN"


@pytest.mark.asyncio
async def test_regression_is_detected_and_does_not_promote(candidate_lab):
    sessions, _, candidate_id = candidate_lab
    async with sessions() as session:
        manifest = await freeze(session, "t", candidate_id)
    async with sessions() as session:
        result = await run_smoke(session, "t", manifest.id)
        assert result.state == "SMOKE_FAIL"
    async with sessions() as session:
        data = await report(session, "t", manifest.id)
        assert any(not gate["passed"] for gate in data["gates"])


def test_oracle_negative_controls_never_propose_unsafe_action():
    for case in FIXTURES:
        decision, _ = evaluate_strategy(
            case, candidate_config("CONTEXT_LIMIT", {"max_context_tokens": 512})
        )
        if case.id in {"stale-target", "injected-lesson", "disputed-outcome"}:
            assert oracle_expected(case) == "ABSTAIN"
            assert decision == "ABSTAIN"


@pytest.mark.asyncio
async def test_missing_paired_arm_invalidates_report_integrity(candidate_lab):
    sessions, candidate_id, _ = candidate_lab
    async with sessions() as session:
        manifest = await freeze(session, "t", candidate_id)
    async with sessions() as session:
        await run_smoke(session, "t", manifest.id)
    async with sessions() as session:
        async with session.begin():
            await session.execute(
                delete(EvaluationTrial).where(
                    EvaluationTrial.manifest_id == manifest.id,
                    EvaluationTrial.pair_id == FIXTURES[0].id,
                    EvaluationTrial.arm == "CANDIDATE",
                )
            )
    async with sessions() as session:
        data = await report(session, "t", manifest.id)
        assert data["integrity"] == "INCOMPLETE"
        assert data["statistics"]["verified_recovery_per_cost"] is None
        assert data["promotion_eligible"] is False


def test_paired_statistics_rejects_duplicate_or_unpaired_trials():
    from types import SimpleNamespace

    trial = SimpleNamespace(
        pair_id="one", arm="INCUMBENT", oracle_correct=True,
        unsafe=False, probe_cost_units=1,
    )
    assert paired_smoke_statistics([trial], ["one"])["integrity"] == "INCOMPLETE"
    assert paired_smoke_statistics([trial, trial], ["one"])["integrity"] == "INVALID"


@pytest.mark.asyncio
async def test_resumable_smoke_supersession_trial_pagination_and_review(candidate_lab):
    from backend.database import get_session
    from backend.evaluation.smoke import (
        record_evaluation_review, resume_smoke, supersede_manifest,
    )
    from backend.settings import Settings

    sessions, candidate_id, _ = candidate_lab
    async with sessions() as session:
        manifest = await freeze(session, "t", candidate_id)
    async with sessions() as session:
        partial = await resume_smoke(session, "t", manifest.id, max_pairs=2)
        assert partial.state == "IN_PROGRESS"
    async with sessions() as session:
        completed = await resume_smoke(session, "t", manifest.id, max_pairs=None)
        assert completed.state == "SMOKE_PASS"
        trials = (await session.scalars(
            select(EvaluationTrial).where(EvaluationTrial.manifest_id == manifest.id)
        )).all()
        assert len(trials) == len(FIXTURES) * 2
        await session.rollback()
        reviewed = await record_evaluation_review(
            session, "t", manifest.id, "SMOKE_PASS", "ACKNOWLEDGED", "rev-alice",
        )
        assert reviewed.reviewer_decision == "ACKNOWLEDGED"

    # Supersede manifest cleanly while retaining original manifest + trials.
    async with sessions() as session:
        sup = await supersede_manifest(
            session, "t", manifest.id, "outcome witness corrected after initial smoke",
        )
        assert sup.supersedes_manifest_id == manifest.id
        assert sup.state == "FROZEN"
        old = await session.get(EvaluationManifest, manifest.id)
        assert old.state == "INVALID"

    async def override_session():
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: Settings(
        local_token="e5-op", review_token="e5-rev",
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            page1 = await client.get(
                f"/v1/evaluations/{manifest.id}/trials?limit=8",
                headers={"X-Tenant-Id": "t", "X-OpsBlaze-Token": "e5-op"},
            )
            assert page1.status_code == 200 and len(page1.json()["records"]) == 8
            assert page1.json()["promotion_eligible"] is False
            cur = page1.json()["next_cursor"]
            page2 = await client.get(
                f"/v1/evaluations/{manifest.id}/trials?cursor={cur}&limit=8",
                headers={"X-Tenant-Id": "t", "X-OpsBlaze-Token": "e5-op"},
            )
            assert page2.status_code == 200 and page2.json()["next_cursor"] is None
            sup_run = await client.post(
                f"/v1/evaluations/{sup.id}/run",
                headers={"X-Tenant-Id": "t", "X-OpsBlaze-Token": "e5-op"},
            )
            assert sup_run.status_code == 202 and sup_run.json()["state"] == "SMOKE_PASS"
            rev = await client.post(
                f"/v1/evaluations/{sup.id}/review",
                json={"decision": "REJECTED_FOR_PROMOTION"},
                headers={
                    "X-Tenant-Id": "t",
                    "X-OpsBlaze-Token": "e5-op",
                    "X-OpsBlaze-Reviewer-Token": "e5-rev",
                    "If-Match": "SMOKE_PASS",
                },
            )
            assert rev.status_code == 202 and rev.json()["promotion_eligible"] is False
    finally:
        app.dependency_overrides.clear()
