"""The workbench receives bounded source details through an authenticated read."""

import asyncio
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.api import app
from backend.database import get_session
from backend.models import DecisionAssessment, Evidence, HypothesisStep, Incident, OutcomeRevision
from backend.schemas import IncidentCreate
from backend.services import admit_incident
from backend.settings import Settings, get_settings


async def test_investigation_refresh_bounds_history_and_redacts_foreign_refs(sessions_fixture, monkeypatch):
    async with sessions_fixture() as session:
        own = await admit_incident(session, "own", "workbench", IncidentCreate(target_id="checkout"))
        other = await admit_incident(session, "other", "workbench", IncidentCreate(target_id="checkout"))
    own_id, foreign_id = uuid4(), uuid4()
    async with sessions_fixture.begin() as session:
        for scope, incident, evidence_id, excerpt in (
            ("own", own, own_id, "allowed redacted excerpt"),
            ("other", other, foreign_id, "foreign-only excerpt"),
        ):
            session.add(Evidence(
                id=evidence_id, tenant_id=scope, incident_id=incident.id, target_id="checkout",
                source="simulator.health", source_version="simulator-v1", validity="VALID",
                classification="INTERNAL", sha256="a" * 64, redacted_excerpt=excerpt,
                observed_at=datetime.now(timezone.utc),
            ))
        session.add_all([
            HypothesisStep(
                id=uuid4(), incident_id=own.id, step_no=index, model_id="deterministic",
                prompt_version="none", decision="ABSTAINED", probe_id="simulator.health",
                cause_scores={"simulated_target_unhealthy": 1, "unapproved-label": 1},
                evidence_ids=[str(own_id), str(foreign_id), {"private": "unsupported raw value"}],
            )
            for index in range(1, 22)
        ])
        session.add(DecisionAssessment(
            id=uuid4(), tenant_id="own", incident_id=own.id, step_no=21, feature_version="lab",
            threshold_version="lab", evidence_digest="0" * 64, risk_score=100,
            reason="TOOL_UNAVAILABLE", outcome="ABSTAINED",
        ))
        session.add(OutcomeRevision(
            id=uuid4(), tenant_id="own", incident_id=own.id, revision=1, label="FAILED",
            reason="internal human feedback", witness_source="SIMULATOR_WITNESS",
        ))

    settings = Settings(local_token="disposable-test", max_probe_calls_per_incident=3)
    async def session_override():
        async with sessions_fixture() as session:
            yield session

    monkeypatch.setitem(app.dependency_overrides, get_session, session_override)
    monkeypatch.setitem(app.dependency_overrides, get_settings, lambda: settings)
    headers = {"X-Tenant-Id": "own", "X-OpsBlaze-Token": settings.local_token}
    path = f"/v1/incidents/{own.id}/investigation"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get(path)).status_code == 422
        assert (await client.get(path, headers={**headers, "X-OpsBlaze-Token": "bad-test-token"})).status_code == 401
        assert (await client.get(path, headers={**headers, "X-Tenant-Id": "other"})).status_code == 404
        response = await client.get(path, headers=headers)
    assert response.status_code == 200, response.text
    assert str(foreign_id) not in response.text
    assert all(value not in response.text for value in (
        "foreign-only excerpt", "unsupported raw value", "internal human feedback", "unapproved-label",
    ))
    body = response.json()
    assert body["incident"]["id"] == str(own.id)
    assert body["probe_budget"] == {"used": 0, "limit": 3, "remaining": 3}
    assert body["steps_truncated"]
    assert [step["step_no"] for step in body["steps"]] == list(range(2, 22))
    last = body["steps"][-1]
    assert last["hypotheses"] == ["simulated_target_unhealthy"]
    assert last["incomplete_evidence"]
    assert [record["id"] for record in last["evidence"]] == [str(own_id)]
    assert body["latest_assessment"] == {"reason": "TOOL_UNAVAILABLE", "outcome": "ABSTAINED", "step_no": 21}
    assert body["recovery"]["latest_outcome_label"] == "FAILED"
    assert body["recovery"]["admission_to_recorded_witness_ms"] is None


@pytest.mark.integration
@pytest.mark.pg_e2e
async def test_postgres_workbench_uses_one_snapshot_during_concurrent_update(monkeypatch):
    from backend import workbench

    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant = f"workbench-{uuid4().hex}"
    opened = asyncio.Event()
    changed = asyncio.Event()
    lookup = workbench.find_incident

    async def paused_lookup(*args):
        row = await lookup(*args)
        opened.set()
        await changed.wait()
        return row

    async def session_override():
        async with sessions() as session:
            yield session

    monkeypatch.setattr(workbench, "find_incident", paused_lookup)
    monkeypatch.setitem(app.dependency_overrides, get_session, session_override)
    try:
        async with sessions() as session:
            incident = await admit_incident(session, tenant, "snapshot", IncidentCreate(target_id="checkout"))
        headers = {"X-Tenant-Id": tenant, "X-OpsBlaze-Token": settings.local_token}
        path = f"/v1/incidents/{incident.id}/investigation"
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            async with asyncio.timeout(10), asyncio.TaskGroup() as tasks:
                first = tasks.create_task(client.get(path, headers=headers))
                await opened.wait()
                async with sessions.begin() as session:
                    live = await session.get(Incident, incident.id)
                    live.probe_used = 1
                    session.add(OutcomeRevision(
                        id=uuid4(), tenant_id=tenant, incident_id=incident.id, revision=1, label="FAILED",
                        reason="synthetic observation", witness_source="SIMULATOR_WITNESS",
                    ))
                changed.set()
            before = first.result().json()
            after = (await client.get(path, headers=headers)).json()
        assert before["probe_budget"]["used"] == 0
        assert before["recovery"]["latest_outcome_label"] is None
        assert after["probe_budget"]["used"] == 1
        assert after["recovery"]["latest_outcome_label"] == "FAILED"
    finally:
        await engine.dispose()
