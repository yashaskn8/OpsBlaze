"""Exercise pinned advisory routing through registered activities and Temporal."""

import asyncio
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from backend.api import app
from backend.database import get_engine, get_session, get_session_maker
from backend.investigator import advisory
from backend.models import DecisionAssessment, Incident, IncidentPlan, ModelAdvisory, RouteDecision, SimulatorTarget
from backend.routing import decision
from backend.schemas import IncidentCreate
from backend.services import admit_incident
from backend.settings import Settings, get_settings
from backend.workflows import activities
from backend.workflows.incident import IncidentWorkflow


class CountingProvider:
    def __init__(self, monkeypatch):
        self.models = []
        self.responses = self
        monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(AsyncOpenAI=lambda **_: self))

    async def parse(self, *, model, **request):
        self.models.append(model)
        assert request["max_output_tokens"] == 128 and request["store"] is False
        return SimpleNamespace(
            status="completed",
            output_parsed={"next_probe": "ABSTAIN", "reason_code": "INSUFFICIENT_EVIDENCE"},
            usage=SimpleNamespace(total_tokens=37),
        )


async def seed(sessions, tenant, target="checkout"):
    async with sessions.begin() as session:
        session.add(SimulatorTarget(tenant_id=tenant, target_id=target, version=1, healthy=False, last_fence=0))
    async with sessions() as session:
        return await admit_incident(session, tenant, uuid4().hex, IncidentCreate(target_id=target))


def configure_advisory(monkeypatch, settings):
    monkeypatch.setattr(decision, "get_settings", lambda: settings)
    monkeypatch.setattr(advisory, "get_settings", lambda: settings)


async def test_runbook_cannot_gain_model_use_and_disabled_pin_cannot_retry(
    sessions_fixture, monkeypatch,
):
    settings = Settings(ai_advisory_enabled=False, ai_advisory_model="local-synthetic")
    configure_advisory(monkeypatch, settings)
    monkeypatch.setattr(activities, "get_session_maker", lambda: sessions_fixture)
    provider = CountingProvider(monkeypatch)
    runbook = await seed(sessions_fixture, "runbook")
    assert await activities.select_incident_route("runbook", str(runbook.id)) == "RUNBOOK"

    settings.ai_advisory_enabled = True
    assert await activities.inspect_incident_read_only("runbook", str(runbook.id)) == "SIMULATOR_REVIEW_REQUIRED"
    pin = await seed(sessions_fixture, "agent")
    assert await activities.select_incident_route("agent", str(pin.id)) == "AGENT_NO_MEMORY"
    settings.ai_advisory_enabled = False
    assert await activities.inspect_incident_read_only("agent", str(pin.id)) == "SIMULATOR_REVIEW_REQUIRED"
    settings.ai_advisory_enabled = True
    assert await activities.inspect_incident_read_only("agent", str(pin.id)) == "SIMULATOR_REVIEW_REQUIRED"
    assert provider.models == []
    async with sessions_fixture() as session:
        assert await session.get(ModelAdvisory, runbook.id) is None
        skipped = await session.get(ModelAdvisory, pin.id)
        assert (skipped.state, skipped.reason_code, skipped.tokens_reserved) == ("SKIPPED", "DISABLED", 0)


async def test_route_digest_changes_with_budget_and_production_never_pins_model(
    sessions_fixture, monkeypatch,
):
    settings = Settings(environment="production", ai_advisory_enabled=True, max_probe_calls_per_incident=3)
    configure_advisory(monkeypatch, settings)
    first = await seed(sessions_fixture, "tenant")
    async with sessions_fixture() as session:
        second = await admit_incident(session, "tenant", "second", IncidentCreate(target_id="checkout"))
    a = await decision.select_route(sessions_fixture, "tenant", first.id)
    settings.max_probe_calls_per_incident = 1
    b = await decision.select_route(sessions_fixture, "tenant", second.id)
    assert (a.route, a.model_id, a.model_token_reservation) == ("RUNBOOK", None, 0)
    assert b.route == "HANDOFF"
    assert a.context_digest != b.context_digest
    assert (await decision.select_route(sessions_fixture, "tenant", first.id)).context_digest == a.context_digest


async def test_unversioned_runbook_only_reuses_completed_legacy_advice(sessions_fixture, monkeypatch):
    settings = Settings(ai_advisory_enabled=False)
    configure_advisory(monkeypatch, settings)
    monkeypatch.setattr(activities, "get_session_maker", lambda: sessions_fixture)
    provider = CountingProvider(monkeypatch)
    incident = await seed(sessions_fixture, "legacy")
    route = await decision.select_route(sessions_fixture, "legacy", incident.id)
    async with sessions_fixture.begin() as session:
        legacy = await session.get(RouteDecision, route.id)
        legacy.router_version = legacy.model_token_reservation = None
        session.add(ModelAdvisory(
            incident_id=incident.id, tenant_id="legacy", model_id="legacy-model", state="COMPLETE",
            next_probe="ABSTAIN", reason_code="INSUFFICIENT_EVIDENCE", tokens_reserved=512, tokens_used=11,
        ))
    settings.ai_advisory_enabled = True
    assert await activities.inspect_incident_read_only("legacy", str(incident.id)) == "ABSTAINED"
    assert provider.models == []


@pytest.mark.integration
@pytest.mark.pg_e2e
async def test_native_advisory_workflow_pins_model_before_use_and_replays(monkeypatch):
    settings = get_settings().model_copy(update={
        "ai_advisory_enabled": True, "ai_advisory_model": "original-test-model", "environment": "development",
    })
    configure_advisory(monkeypatch, settings)
    provider = CountingProvider(monkeypatch)
    sessions = get_session_maker()
    tenant = f"pinned-route-{uuid4().hex}"
    incident = await seed(sessions, tenant)
    try:
        route = await decision.select_route(sessions, tenant, incident.id)
        assert route.route == "AGENT_NO_MEMORY"
        assert not provider.models
        settings.ai_advisory_model = "ambient-replacement"
        async with await WorkflowEnvironment.start_local() as env:
            queue = f"advisory-{uuid4().hex}"
            async with Worker(
                env.client, task_queue=queue, workflows=[IncidentWorkflow],
                activities=[
                    activities.project_incident_status, activities.select_incident_route,
                    activities.inspect_incident_read_only, activities.apply_approved_simulator_plan,
                ],
            ):
                handle = await env.client.start_workflow(
                    IncidentWorkflow.run, args=[tenant, str(incident.id)], id=queue, task_queue=queue,
                )
                assert await asyncio.wait_for(handle.result(), 30) == "NEEDS_REVIEW"
                history = await handle.fetch_history()
        await Replayer(workflows=[IncidentWorkflow]).replay_workflow(history)
        assert await activities.inspect_incident_read_only(tenant, str(incident.id)) == "ABSTAINED"
        assert provider.models == ["original-test-model"]
        async with sessions() as session:
            assert (await session.get(Incident, incident.id)).status_projection == "NEEDS_REVIEW"
            stored = await session.get(RouteDecision, route.id)
            assert (stored.model_id, stored.model_token_reservation) == ("original-test-model", 512)
            advice = await session.get(ModelAdvisory, incident.id)
            assert (advice.state, advice.tokens_used) == ("COMPLETE", 37)
            assessment = await session.scalar(select(DecisionAssessment).where(
                DecisionAssessment.incident_id == incident.id,
            ))
            assert (assessment.outcome, assessment.reason) == ("ABSTAINED", "MODEL_UNCERTAINTY")
            assert await session.scalar(select(IncidentPlan).where(IncidentPlan.incident_id == incident.id)) is None

        async def session_override():
            async with sessions() as session:
                yield session
        monkeypatch.setitem(app.dependency_overrides, get_session, session_override)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            headers = {"X-Tenant-Id": tenant, "X-OpsBlaze-Token": settings.local_token}
            path = f"/v1/incidents/{incident.id}/route"
            response = await client.get(path, headers=headers)
            assert response.status_code == 200
            assert response.json()["model_id"] == "original-test-model"
            assert response.json()["router_version"] == decision.ROUTER_VERSION
            assert (await client.get(path, headers={**headers, "X-Tenant-Id": "other"})).status_code == 404
    finally:
        await get_engine().dispose()
