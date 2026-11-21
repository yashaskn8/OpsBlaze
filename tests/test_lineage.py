
from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.investigator.baseline import inspect
from backend.lineage import build_lineage, read_lineage_page
from backend.models import HypothesisStep, SimulatorTarget
from backend.schemas import IncidentCreate
from backend.services import admit_incident


@pytest.mark.asyncio
async def test_tenant_scoped_lineage_root(sessions_fixture):
    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "t", "root", IncidentCreate(target_id="checkout")
        )
    async with sessions_fixture() as session:
        graph = await build_lineage(session, "t", incident.id)
        assert graph["projection"] == "COMPLETE"
        assert graph["nodes"][0]["id"] == f"incident:{incident.id}"
        assert graph["trace_spans_available"] is False
        assert await build_lineage(session, "other", incident.id) is None


@pytest.mark.asyncio
async def test_evidence_to_plan_links_are_persistently_reconstructed(sessions_fixture):
    async with sessions_fixture() as session:
        async with session.begin():
            session.add(
                SimulatorTarget(
                    tenant_id="t", target_id="checkout",
                    version=1, healthy=False, last_fence=0,
                )
            )
    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "t", "link", IncidentCreate(target_id="checkout")
        )
    assert await inspect(sessions_fixture, "t", incident.id) == "SIMULATOR_REVIEW_REQUIRED"
    async with sessions_fixture() as session:
        graph = await build_lineage(session, "t", incident.id)
        assert graph["projection"] == "COMPLETE"
        assert len([n for n in graph["nodes"] if n["kind"] == "evidence"]) == 2
        assert len([n for n in graph["nodes"] if n["kind"] == "plan"]) == 1
        assert len([e for e in graph["edges"] if e["relation"] == "JUSTIFIED_BY"]) == 2


@pytest.mark.asyncio
async def test_missing_evidence_reference_is_reported_not_invented(sessions_fixture):
    async with sessions_fixture() as session:
        async with session.begin():
            session.add(
                SimulatorTarget(
                    tenant_id="t", target_id="checkout",
                    version=1, healthy=False, last_fence=0,
                )
            )
    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "t", "broken", IncidentCreate(target_id="checkout")
        )
    await inspect(sessions_fixture, "t", incident.id)
    phantom = str(uuid4())
    async with sessions_fixture() as session:
        async with session.begin():
            step = await session.scalar(
                select(HypothesisStep).where(
                    HypothesisStep.incident_id == incident.id,
                    HypothesisStep.step_no == 1,
                )
            )
            step.evidence_ids = [phantom]
    async with sessions_fixture() as session:
        graph = await build_lineage(session, "t", incident.id)
        assert graph["projection"] == "PARTIAL_LINKS"
        assert any(m["missing_target"] == f"evidence:{phantom}" for m in graph["missing_refs"])
        assert not any(
            n["id"] == f"evidence:{phantom}" for n in graph["nodes"]
        )


@pytest.mark.asyncio
async def test_redteam_large_lineage_never_claims_complete_when_truncated(sessions_fixture):
    """P3 must report omitted records instead of disguising a partial graph."""
    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "tenant", "many-steps",
            IncidentCreate(target_id="checkout"),
        )
    async with sessions_fixture() as session:
        async with session.begin():
            session.add_all([
                HypothesisStep(
                    id=uuid4(), incident_id=incident.id, step_no=i,
                    model_id="deterministic-baseline-v1", prompt_version="none",
                    evidence_ids=[], cause_scores={}, probe_id="simulator.health",
                    decision="ABSTAINED", token_reserved=0, token_used=0,
                )
                for i in range(1, 52)
            ])
    async with sessions_fixture() as session:
        graph = await build_lineage(session, "tenant", incident.id)
        assert graph["projection"] == "PARTIAL_LINKS"
        assert graph["truncated_sources"] == ["hypothesis"]
        assert len([n for n in graph["nodes"] if n["kind"] == "hypothesis"]) == 50
        assert await build_lineage(session, "foreign", incident.id) is None

        first = await read_lineage_page(
            session, "tenant", incident.id, "hypothesis", limit=50,
        )
        assert len(first["nodes"]) == 50 and first["next_cursor"]
        second = await read_lineage_page(
            session, "tenant", incident.id, "hypothesis",
            cursor=__import__("uuid").UUID(first["next_cursor"]), limit=50,
        )
        assert len(second["nodes"]) == 1 and second["next_cursor"] is None
        assert not {n["id"] for n in first["nodes"]}.intersection(
            n["id"] for n in second["nodes"]
        )
        assert await read_lineage_page(
            session, "foreign", incident.id, "hypothesis",
        ) is None


@pytest.mark.asyncio
async def test_redteam_approval_grant_is_part_of_effect_causal_chain(sessions_fixture):
    from datetime import datetime, timedelta, timezone

    from backend.models import ApprovalGrant, EffectReceipt, IncidentPlan

    async with sessions_fixture() as session:
        async with session.begin():
            session.add(SimulatorTarget(
                tenant_id="t", target_id="checkout", version=1,
                healthy=False, last_fence=0,
            ))
    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "t", "causal-approval",
            IncidentCreate(target_id="checkout"),
        )
    assert await inspect(sessions_fixture, "t", incident.id) == "SIMULATOR_REVIEW_REQUIRED"
    async with sessions_fixture() as session:
        plan = await session.scalar(select(IncidentPlan))
        plan_id, digest = plan.id, plan.plan_digest

    grant_id, effect_id = uuid4(), uuid4()
    async with sessions_fixture() as session:
        async with session.begin():
            session.add(ApprovalGrant(
                id=grant_id, tenant_id="t", plan_id=plan_id,
                plan_digest=digest, reviewer_id="DEV_REVIEWER_CREDENTIAL",
                scope="SIMULATOR_MARK_HEALTHY", decision="APPROVE",
                state="CONSUMED", reason="authorized synthetic trace",
                version=2,
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            ))
            session.add(EffectReceipt(
                id=effect_id, tenant_id="t", incident_id=incident.id,
                plan_digest=digest, target_id="checkout",
                adapter_id="simulator", idempotency_key=str(effect_id),
                fence=1, expected_version=1, state="SENT",
                attempt_count=1,
            ))
    async with sessions_fixture() as session:
        graph = await build_lineage(session, "t", incident.id)
    assert graph["projection"] == "COMPLETE"
    assert any(n["id"] == f"approval:{grant_id}" for n in graph["nodes"])
    assert {
        (e["from"], e["to"], e["relation"]) for e in graph["edges"]
    } >= {
        (f"approval:{grant_id}", f"plan:{plan_id}", "REVIEWS"),
        (f"effect:{effect_id}", f"approval:{grant_id}", "AUTHORIZED_BY_GRANT"),
    }
    async with sessions_fixture() as session:
        assert await build_lineage(session, "wrong-tenant", incident.id) is None

    # A durable grant remains visible in historical provenance, but a
    # rejected or digest-mismatched grant must NEVER be marked as authority.
    for field, value in (("state", "REJECTED"), ("plan_digest", "f" * 64)):
        async with sessions_fixture() as session:
            async with session.begin():
                grant = await session.get(ApprovalGrant, grant_id)
                grant.state = "CONSUMED"
                grant.plan_digest = digest
                setattr(grant, field, value)
        async with sessions_fixture() as session:
            broken = await build_lineage(session, "t", incident.id)
        assert broken["projection"] == "PARTIAL_LINKS"
        assert any(
            m["source"] == f"effect:{effect_id}"
            and m["relation"] == "AUTHORIZED_BY_GRANT"
            for m in broken["missing_refs"]
        )
        assert not any(
            e["from"] == f"effect:{effect_id}"
            and e["relation"] == "AUTHORIZED_BY_GRANT"
            for e in broken["edges"]
        )


@pytest.mark.asyncio
async def test_redteam_lineage_page_api_rejects_cross_tenant_and_unbounded_queries(
    sessions_fixture,
):
    from httpx import ASGITransport, AsyncClient

    from backend.api import app
    from backend.database import get_session
    from backend.settings import Settings, get_settings

    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "t", "paged-api", IncidentCreate(target_id="checkout"),
        )

    async def session_override():
        async with sessions_fixture() as session:
            yield session

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_settings] = lambda: Settings(
        local_token="paged-lineage-test-token",
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as client:
            url = f"/v1/incidents/{incident.id}/lineage/records"
            own = {"X-Tenant-Id": "t", "X-OpsBlaze-Token": "paged-lineage-test-token"}
            result = await client.get(url, params={"kind": "evidence", "limit": 10},
                                      headers=own)
            assert result.status_code == 200
            assert result.json()["nodes"] == []
            assert result.json()["next_cursor"] is None
            wrong = {**own, "X-Tenant-Id": "other"}
            assert (await client.get(url, params={"kind": "evidence"},
                                     headers=wrong)).status_code == 404
            assert (await client.get(url, params={"kind": "effect", "limit": 51},
                                     headers=own)).status_code == 422
            assert (await client.get(url, params={"kind": "unbounded-internal"},
                                     headers=own)).status_code == 422
            assert (await client.get(url, params={
                "kind": "evidence", "cursor": "not-a-uuid",
            }, headers=own)).status_code == 422
    finally:
        app.dependency_overrides.clear()
