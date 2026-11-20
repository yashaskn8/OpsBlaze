
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
