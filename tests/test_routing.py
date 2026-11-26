
import pytest
from sqlalchemy import select

from backend.models import PolicyAssignment, RouteDecision, SimulatorTarget
from backend.routing.decision import ROUTER_POLICY_ID, select_route
from backend.schemas import IncidentCreate
from backend.services import admit_incident


@pytest.mark.asyncio
async def test_route_is_pinned_and_tenant_scoped(sessions_fixture):
    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "t", "route", IncidentCreate(target_id="checkout")
        )
    first = await select_route(sessions_fixture, "t", incident.id)
    assert first.route == "HANDOFF"
    assert first.selection_reason == "SIMULATOR_TARGET_MISSING"
    async with sessions_fixture() as session:
        async with session.begin():
            session.add(
                SimulatorTarget(
                    tenant_id="t", target_id="checkout",
                    version=1, healthy=False, last_fence=0,
                )
            )
    second = await select_route(sessions_fixture, "t", incident.id)
    assert second.id == first.id
    assert second.route == "HANDOFF"
    assert await select_route(sessions_fixture, "other", incident.id) is None
    async with sessions_fixture() as session:
        assert len((await session.scalars(select(RouteDecision))).all()) == 1


@pytest.mark.asyncio
async def test_simulator_route_never_uses_model_or_memory(sessions_fixture):
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
            session, "t", "route", IncidentCreate(target_id="checkout")
        )
    selected = await select_route(sessions_fixture, "t", incident.id)
    assert selected.policy_id == ROUTER_POLICY_ID
    assert selected.route == "RUNBOOK"
    assert selected.propensity == 1
    assert selected.cost_cap_probes == 2


@pytest.mark.asyncio
async def test_missing_or_changed_policy_pin_forces_handoff(sessions_fixture):
    async with sessions_fixture() as session:
        async with session.begin():
            session.add(SimulatorTarget(
                tenant_id="t", target_id="checkout", version=1,
                healthy=False, last_fence=0,
            ))
    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "t", "wrong-pin", IncidentCreate(target_id="checkout")
        )
    async with sessions_fixture() as session:
        async with session.begin():
            pin = await session.get(PolicyAssignment, ("t", incident.id))
            pin.policy_id = "unapproved-model-v1"
    chosen = await select_route(sessions_fixture, "t", incident.id)
    assert chosen.route == "HANDOFF"
    assert chosen.selection_reason == "POLICY_PIN_INVALID"
    assert chosen.cost_cap_probes == 0


@pytest.mark.asyncio
async def test_e7_contextual_risk_freshness_memory_and_fallback_recovery(
    sessions_fixture, monkeypatch,
):
    """E7-04, E7-05, E7-07: unresolved effect risk forces HANDOFF, persisted features, memory failure fallback & operator handoff."""
    from datetime import datetime, timezone
    from uuid import uuid4
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy.exc import OperationalError

    from backend.api import app
    from backend.database import get_session
    from backend.models import EffectReceipt, Evidence
    from backend.settings import Settings, get_settings

    async with sessions_fixture() as session:
        async with session.begin():
            session.add(SimulatorTarget(
                tenant_id="t", target_id="checkout", version=1, healthy=False, last_fence=0,
            ))
    async with sessions_fixture() as session:
        inc_clean = await admit_incident(session, "t", "clean-ctx", IncidentCreate(target_id="checkout"))
    async with sessions_fixture() as session:
        inc_risky = await admit_incident(session, "t", "risky-ctx", IncidentCreate(target_id="checkout"))

    # Add fresh evidence to inc_clean and simulate memory-store failure -> deterministic no-memory fallback.
    async with sessions_fixture() as session:
        async with session.begin():
            session.add(Evidence(
                id=uuid4(), tenant_id="t", incident_id=inc_clean.id, target_id="checkout",
                source="simulator.health", source_version="simulator-v1", target_version=1,
                observed_at=datetime.now(timezone.utc), sha256="a" * 64,
                redacted_excerpt='{"healthy":false}', classification="INTERNAL", validity="VALID",
            ))

    async def broken_lessons(*args, **kwargs):
        raise OperationalError("select 1", {}, Exception("memory store offline"))

    monkeypatch.setattr("backend.routing.decision.retrieve_active_lessons", broken_lessons)
    clean_route = await select_route(sessions_fixture, "t", inc_clean.id)
    assert clean_route.route == "RUNBOOK"
    assert clean_route.context_features["source_freshness"] == "FRESH"
    assert clean_route.context_features["memory_status"] == "MEMORY_FALLBACK_UNAVAILABLE"
    assert clean_route.context_features["eligible_memory_count"] == 0

    # An active unresolved effect on the target elevates risk and forces deterministic HANDOFF.
    eff_id = uuid4()
    async with sessions_fixture() as session:
        async with session.begin():
            session.add(EffectReceipt(
                id=eff_id, tenant_id="t", incident_id=inc_clean.id, plan_digest="d" * 64,
                target_id="checkout", adapter_id="simulator", idempotency_key=str(eff_id),
                fence=1, expected_version=1, state="UNKNOWN", attempt_count=1,
            ))
    risky_route = await select_route(sessions_fixture, "t", inc_risky.id)
    assert risky_route.route == "HANDOFF"
    assert risky_route.selection_reason == "UNRESOLVED_EFFECT_ACTIVE"
    assert risky_route.risk_band == "ELEVATED_UNRESOLVED_EFFECT"
    assert risky_route.cost_cap_probes == 0
    assert risky_route.context_features["unresolved_effects_on_target"] == 1

    # Authorized operator handoff API and route detail inspection.
    async def override_session():
        async with sessions_fixture() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: Settings(local_token="route-tok")
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            own = {"X-Tenant-Id": "t", "X-OpsBlaze-Token": "route-tok"}
            route_res = await client.get(f"/v1/incidents/{inc_risky.id}/route", headers=own)
            assert route_res.status_code == 200
            assert route_res.json()["context_features"]["unresolved_effects_on_target"] == 1
            assert len(route_res.json()["context_digest"]) == 64

            handoff = await client.post(
                f"/v1/incidents/{inc_clean.id}/handoff",
                headers={**own, "If-Match": "1"},
            )
            assert handoff.status_code == 202
            assert handoff.json()["status_projection"] == "NEEDS_REVIEW"
            # Stale version rejected.
            assert (await client.post(
                f"/v1/incidents/{inc_clean.id}/handoff",
                headers={**own, "If-Match": "1"},
            )).status_code == 409
    finally:
        app.dependency_overrides.clear()
