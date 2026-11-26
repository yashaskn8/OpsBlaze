"""E1 expiry must scrub content and revoke dependent memory."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.experiences import capture_capsule, list_capsules, set_consent
from backend.investigator.abstain import assess_decision
from backend.memory.lessons import create_draft, retrieve_active_lessons
from backend.models import (
    Evidence, EvidenceAccessAudit, ExperienceCapsule, OperationalLesson,
    OutcomeRevision, SimulatorTarget,
)
from backend.probes.gateway import (
    EXPIRED_EXCERPT, classify_evidence_freshness, invoke_probe,
)
from backend.schemas import IncidentCreate
from backend.services import admit_incident
from backend.workers.retention import expire_capsules, expire_evidence_payloads


@pytest.mark.asyncio
async def test_expired_capsule_scrub_and_lesson_revocation(sessions_fixture):
    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "tenant", "expiry", IncidentCreate(target_id="checkout")
        )
    async with sessions_fixture() as session:
        await set_consent(session, "tenant", incident.id, 0, True)
    async with sessions_fixture() as session:
        async with session.begin():
            session.add(OutcomeRevision(
                id=uuid4(), tenant_id="tenant", incident_id=incident.id,
                effect_id=None, revision=1, label="VERIFIED",
                witness_source="SIMULATOR_WITNESS", observed_version=2,
                reason="isolated fixture",
            ))
    async with sessions_fixture() as session:
        capsule = await capture_capsule(session, "tenant", incident.id)
    async with sessions_fixture() as session:
        lesson = await create_draft(
            session, "tenant", capsule.id, "CHECK_VERSION"
        )
    cutoff = datetime.now(timezone.utc)
    async with sessions_fixture() as session:
        async with session.begin():
            stored = await session.get(ExperienceCapsule, capsule.id)
            stored.expires_at = cutoff - timedelta(seconds=1)
    result = await expire_capsules(sessions_fixture, now=cutoff)
    assert result == {"capsules_scrubbed": 1, "lessons_revoked": 1}
    assert await expire_capsules(sessions_fixture, now=cutoff) == {
        "capsules_scrubbed": 0, "lessons_revoked": 0,
    }
    async with sessions_fixture() as session:
        expired = await session.get(ExperienceCapsule, capsule.id)
        revoked = await session.get(OperationalLesson, lesson.id)
        assert expired.status == "EXPIRED"
        assert expired.target_digest == "" and expired.evidence_ids == []
        assert revoked.status == "REVOKED" and revoked.target_digest == ""
        assert await list_capsules(session, "tenant") == []
        assert await retrieve_active_lessons(session, "tenant", "checkout") == []


@pytest.mark.asyncio
async def test_evidence_lifecycle_expiry_access_audit_and_abstention(
    sessions_fixture, monkeypatch,
):
    from httpx import ASGITransport, AsyncClient
    from backend.api import app
    from backend.auth import local_tenant
    from backend.database import get_session

    async with sessions_fixture.begin() as session:
        session.add(SimulatorTarget(
            tenant_id="tenant-ev", target_id="checkout", version=1, healthy=False, last_fence=0,
        ))
    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "tenant-ev", "ev-life-1", IncidentCreate(target_id="checkout"),
        )
    health = await invoke_probe(sessions_fixture, "tenant-ev", incident.id, 1, "simulator.health")
    version = await invoke_probe(sessions_fixture, "tenant-ev", incident.id, 2, "simulator.version")
    assert health.evidence_id and version.evidence_id

    async def override_session():
        async with sessions_fixture() as session:
            yield session

    monkeypatch.setitem(app.dependency_overrides, get_session, override_session)
    monkeypatch.setitem(app.dependency_overrides, local_tenant, lambda: "tenant-ev")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        listed = await client.get(f"/v1/incidents/{incident.id}/evidence")
        assert listed.status_code == 200 and len(listed.json()) == 2
        assert all(item["freshness"] == "FRESH" for item in listed.json())

        detail = await client.get(
            f"/v1/incidents/{incident.id}/lineage/record/evidence/{health.evidence_id}"
        )
        assert detail.status_code == 200

        lifecycle_before = await client.get(f"/v1/incidents/{incident.id}/evidence/lifecycle")
        assert lifecycle_before.status_code == 200
        body = lifecycle_before.json()
        assert (body["total"], body["fresh"], body["expired"]) == (2, 2, 0)
        assert body["access_audit_count"] == 3

    cutoff = datetime.now(timezone.utc) + timedelta(days=31)
    scrubbed = await expire_evidence_payloads(sessions_fixture, now=cutoff)
    assert scrubbed == {"evidence_scrubbed": 2}
    assert await expire_evidence_payloads(sessions_fixture, now=cutoff) == {"evidence_scrubbed": 0}

    async with sessions_fixture() as session:
        ev = await session.get(Evidence, health.evidence_id)
        assert ev is not None
        assert ev.validity == "EXPIRED"
        assert ev.redacted_excerpt == EXPIRED_EXCERPT
        assert len(ev.sha256) == 64  # History digest preserved.
        assert classify_evidence_freshness(ev) == "EXPIRED"
        audits = (await session.scalars(
            select(EvidenceAccessAudit).where(EvidenceAccessAudit.incident_id == incident.id)
        )).all()
        assert len(audits) == 3

    assessment = await assess_decision(
        sessions_fixture, "tenant-ev", incident.id, 2,
        [str(health.evidence_id), str(version.evidence_id)],
    )
    assert assessment is not None
    assert assessment.outcome == "ABSTAINED"
    assert assessment.reason == "EXPIRED_EVIDENCE"


@pytest.mark.parametrize("use_postgres", [False, pytest.param(True, marks=[pytest.mark.integration, pytest.mark.pg_e2e])])
async def test_expired_evidence_is_unavailable_before_scrub(sessions_fixture, monkeypatch, use_postgres):
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from backend.api import app
    from backend.auth import local_tenant
    from backend.database import get_session
    from backend.investigator.baseline import inspect
    from backend.models import Incident, IncidentPlan
    from backend.plans import PlanConflict, decide_plan
    from backend.probes.gateway import ProbeDenied
    from backend.settings import get_settings

    tenant = f"expiry-{uuid4().hex[:12]}"
    engine = create_async_engine(get_settings().database_url) if use_postgres else None
    sessions = async_sessionmaker(engine, expire_on_commit=False) if engine else sessions_fixture
    try:
        async with sessions.begin() as session:
            session.add(SimulatorTarget(tenant_id=tenant, target_id="checkout", version=1, healthy=False, last_fence=0))
        async with sessions() as session:
            incident = await admit_incident(session, tenant, "expiry", IncidentCreate(target_id="checkout"))
        assert await inspect(sessions, tenant, incident.id) == "SIMULATOR_REVIEW_REQUIRED"
        async with sessions.begin() as session:
            health = await session.scalar(select(Evidence).where(
                Evidence.incident_id == incident.id, Evidence.source == "simulator.health",
            ))
            evidence_id = health.id
            assert health.validity == "VALID" and health.redacted_excerpt == '{"healthy":false}'
            health.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            row = await session.get(Incident, incident.id)
            row.status_projection = "AWAITING_APPROVAL"
            plan_id = await session.scalar(select(IncidentPlan.id).where(IncidentPlan.incident_id == incident.id))

        async def override_session():
            async with sessions() as session:
                yield session

        monkeypatch.setitem(app.dependency_overrides, get_session, override_session)
        monkeypatch.setitem(app.dependency_overrides, local_tenant, lambda: tenant)
        path = f"/v1/incidents/{incident.id}"
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            wrappers = ("evidence", "evidence/page", "investigation", f"lineage/record/evidence/{evidence_id}")
            for suffix in wrappers:
                response = await client.get(f"{path}/{suffix}")
                assert response.status_code == 200, response.text
                assert EXPIRED_EXCERPT in response.text, suffix
                assert 'healthy\\\":false' not in response.text, suffix

        with pytest.raises(ProbeDenied, match="expired"):
            await invoke_probe(sessions, tenant, incident.id, 1, "simulator.health")
        async with sessions() as session:
            with pytest.raises(PlanConflict, match="evidence"):
                await decide_plan(session, tenant, plan_id, 1, "APPROVE")
        async with sessions() as session:
            unswept = await session.get(Evidence, evidence_id)
            assert unswept.validity == "VALID" and unswept.redacted_excerpt == '{"healthy":false}'
    finally:
        if engine:
            await engine.dispose()
