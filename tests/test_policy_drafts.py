
from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.models import (
    EvaluationManifest, FailureCluster, ImprovementCandidate, PolicyBundle,
    PolicyRollout, PolicyAssignment, PolicyShadowObservation,
)
from backend.policy.registry import (
    PolicyConflict, create_rollout, dry_run_assignment, register_bundle,
    stop_rollout, record_shadow_observation, shadow_summary,
)
from backend.evaluation.fixtures import fixture_digest, ORACLE_VERSION
from backend.evaluation.smoke import sha


@pytest.fixture
async def safe_evaluation(sessions_fixture):
    cluster_id = uuid4()
    candidate_id = uuid4()
    eval_id = uuid4()
    change = {"max_context_tokens": 512}
    async with sessions_fixture() as session:
        async with session.begin():
            session.add(FailureCluster(
                id=cluster_id, tenant_id="tenant", signature="source",
                version=1, case_count=1, quarantined=False,
            ))
            session.add(ImprovementCandidate(
                id=candidate_id, tenant_id="tenant",
                source_cluster_id=cluster_id, kind="CONTEXT_LIMIT",
                base_policy_id="baseline-v1", change_spec=change,
                hypothesis="maintain sufficient context", risk="LOW",
                evaluation_manifest_id="fixed-smoke-v1",
                state="EVALUATION_READY", version=2,
                created_by="local_operator",
            ))
            session.add(EvaluationManifest(
                id=eval_id, tenant_id="tenant", candidate_id=candidate_id,
                candidate_version=2,
                candidate_digest=sha({"kind": "CONTEXT_LIMIT", "change": change}),
                incumbent_id="baseline-v1", fixture_digest=fixture_digest(),
                oracle_version=ORACLE_VERSION,
                trial_budget=12, state="SMOKE_PASS",
            ))
    return sessions_fixture, candidate_id, eval_id


@pytest.mark.asyncio
async def test_policy_digest_is_frozen_and_does_not_enable_exposure(safe_evaluation):
    sessions, candidate_id, eval_id = safe_evaluation
    async with sessions() as session:
        first = await register_bundle(session, "tenant", candidate_id, eval_id)
        assert first.state == "DRAFT"
    async with sessions() as session:
        second = await register_bundle(session, "tenant", candidate_id, eval_id)
        assert second.id == first.id
        assert second.content["tool_catalog"] == "simulator-readonly-v1"
    async with sessions() as session:
        rollout = await create_rollout(session, "tenant", first.id, 5)
        assert rollout.state == "DRAFT"
        first_assignment = dry_run_assignment(rollout, "tenant", uuid4())
        assert type(first_assignment) is bool
        assert dry_run_assignment(rollout, "another-tenant", uuid4()) is False
    async with sessions() as session:
        paused = await stop_rollout(session, "tenant", rollout.id, 1, "PAUSED")
        assert paused.state == "PAUSED" and paused.version == 2
    async with sessions() as session:
        with pytest.raises(PolicyConflict):
            await stop_rollout(session, "tenant", rollout.id, 1, "ROLLED_BACK")
    async with sessions() as session:
        rolled = await stop_rollout(session, "tenant", rollout.id, 2, "ROLLED_BACK")
        assert rolled.state == "ROLLED_BACK" and rolled.version == 3


@pytest.mark.asyncio
async def test_rollout_scope_and_cohort_are_restricted(safe_evaluation):
    sessions, candidate_id, eval_id = safe_evaluation
    async with sessions() as session:
        bundle = await register_bundle(session, "tenant", candidate_id, eval_id)
    async with sessions() as session:
        with pytest.raises(PolicyConflict):
            await create_rollout(session, "tenant", bundle.id, 100)
    async with sessions() as session:
        with pytest.raises(PolicyConflict):
            await create_rollout(session, "other", bundle.id, 1)


@pytest.mark.asyncio
async def test_shadow_preview_is_idempotent_and_never_changes_assignment(
    safe_evaluation,
):
    from backend.schemas import IncidentCreate
    from backend.services import admit_incident
    from backend.policy.constants import INCUMBENT_POLICY_ID

    sessions, candidate_id, eval_id = safe_evaluation
    async with sessions() as session:
        bundle = await register_bundle(session, "tenant", candidate_id, eval_id)
    async with sessions() as session:
        rollout = await create_rollout(session, "tenant", bundle.id, 5)
    async with sessions() as session:
        incident = await admit_incident(
            session, "tenant", "shadow-incident",
            IncidentCreate(target_id="checkout"),
        )

    async with sessions() as session:
        record = await record_shadow_observation(
            session, "tenant", rollout.id, incident.id,
        )
    assert record.would_assign == dry_run_assignment(
        rollout, "tenant", incident.id,
    )
    async with sessions() as session:
        repeat = await record_shadow_observation(
            session, "tenant", rollout.id, incident.id,
        )
    assert repeat.id == record.id

    async with sessions() as session:
        assignment = await session.get(
            PolicyAssignment, ("tenant", incident.id),
        )
        shadow_rows = (await session.scalars(
            select(PolicyShadowObservation)
        )).all()
        summary = await shadow_summary(session, "tenant", rollout.id)
        assert len(shadow_rows) == 1
        assert assignment.policy_id == INCUMBENT_POLICY_ID
        assert assignment.bundle_id is None and assignment.rollout_id is None
        assert summary["shadow_count"] == 1
        assert summary["candidate_exposure"] == 0
        assert summary["promotion_eligible"] is False

    async with sessions() as session:
        await stop_rollout(session, "tenant", rollout.id, 1, "PAUSED")
    async with sessions() as session:
        with pytest.raises(PolicyConflict, match="stopped"):
            await record_shadow_observation(
                session, "tenant", rollout.id, incident.id,
            )


@pytest.mark.asyncio
async def test_shadow_rejects_wrong_tenant_and_tampered_bundle(safe_evaluation):
    from backend.schemas import IncidentCreate
    from backend.services import admit_incident

    sessions, candidate_id, eval_id = safe_evaluation
    async with sessions() as session:
        bundle = await register_bundle(session, "tenant", candidate_id, eval_id)
    async with sessions() as session:
        rollout = await create_rollout(session, "tenant", bundle.id, 5)
    async with sessions() as session:
        incident = await admit_incident(
            session, "tenant", "shadow-altered",
            IncidentCreate(target_id="checkout"),
        )
    async with sessions() as session:
        assert await record_shadow_observation(
            session, "other", rollout.id, incident.id,
        ) is None

    async with sessions() as session:
        async with session.begin():
            original = await session.get(PolicyBundle, bundle.id)
            original.content = {**original.content, "tool_catalog": "shell.exec"}
    async with sessions() as session:
        with pytest.raises(PolicyConflict, match="digest"):
            await record_shadow_observation(
                session, "tenant", rollout.id, incident.id,
            )
    async with sessions() as session:
        assert (await session.scalars(select(PolicyShadowObservation))).all() == []


@pytest.mark.asyncio
async def test_shadow_http_tenant_permissions_and_live_gate_remain_closed(
    safe_evaluation,
):
    from httpx import ASGITransport, AsyncClient

    from backend.api import app
    from backend.database import get_session
    from backend.schemas import IncidentCreate
    from backend.services import admit_incident
    from backend.settings import Settings, get_settings

    sessions, candidate_id, eval_id = safe_evaluation
    async with sessions() as session:
        bundle = await register_bundle(session, "tenant", candidate_id, eval_id)
    async with sessions() as session:
        rollout = await create_rollout(session, "tenant", bundle.id, 5)
    async with sessions() as session:
        incident = await admit_incident(
            session, "tenant", "shadow-http", IncidentCreate(target_id="checkout"),
        )

    async def override_session():
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: Settings(
        local_token="test-operator", review_token="separate-reviewer",
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as client:
            headers = {
                "X-Tenant-Id": "tenant", "X-OpsBlaze-Token": "test-operator",
            }
            route = f"/v1/rollouts/{rollout.id}/shadow/{incident.id}"
            response = await client.post(route, headers=headers)
            assert response.status_code == 202, response.text
            assert response.json()["exposure_enabled"] is False

            repeated = await client.post(route, headers=headers)
            assert repeated.status_code == 202
            assert repeated.json()["id"] == response.json()["id"]
            summary = await client.get(
                f"/v1/rollouts/{rollout.id}/shadow-summary", headers=headers,
            )
            assert summary.status_code == 200
            assert summary.json()["shadow_count"] == 1
            assert summary.json()["candidate_exposure"] == 0

            blocked = await client.post(
                f"/v1/rollouts/{rollout.id}/approve", headers=headers,
            )
            assert blocked.status_code == 409

            incumbent = await client.get(
                f"/v1/incidents/{incident.id}/policy", headers=headers,
            )
            assert incumbent.status_code == 200
            assert incumbent.json()["bundle_id"] is None
            assert incumbent.json()["rollout_id"] is None

            wrong = {
                "X-Tenant-Id": "unrelated", "X-OpsBlaze-Token": "test-operator",
            }
            assert (await client.post(route, headers=wrong)).status_code == 404
            assert (await client.get(
                f"/v1/rollouts/{rollout.id}/shadow-summary", headers=wrong,
            )).status_code == 404
    finally:
        app.dependency_overrides.clear()
