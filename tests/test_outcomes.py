
import pytest
from sqlalchemy import select

from backend.adapters.simulator import SimulatorAdapter
from backend.effects.gateway import execute_simulator_effect
from backend.effects.reservations import reserve_simulator_target
from backend.models import (
    EffectReceipt, OutcomeRevision, ResourceEpoch, SimulatorTarget
)
from backend.outcomes import OutcomeConflict, adjudicate_simulator, dispute_outcome
from backend.schemas import IncidentCreate
from backend.services import admit_incident


@pytest.fixture
async def witnessed_effect(sessions_fixture):
    async with sessions_fixture() as session:
        async with session.begin():
            session.add(SimulatorTarget(tenant_id="a", target_id="checkout", version=1, healthy=False, last_fence=0))
            session.add(ResourceEpoch(tenant_id="a", target_id="checkout", latest_fence=0, version_seen=1))
    async with sessions_fixture() as session:
        incident = await admit_incident(session, "a", "outcome", IncidentCreate(target_id="checkout"))
    permit = await reserve_simulator_target(sessions_fixture, "a", "checkout", incident.id, "a" * 64)
    adapter = SimulatorAdapter(sessions_fixture)
    assert await execute_simulator_effect(sessions_fixture, adapter, permit) == "VERIFIED"
    async with sessions_fixture() as session:
        receipt = await session.scalar(select(EffectReceipt))
    return sessions_fixture, adapter, incident.id, receipt.id


@pytest.mark.asyncio
async def test_independent_witness_and_dispute_revisions(witnessed_effect):
    sessions, adapter, incident_id, effect_id = witnessed_effect
    assert await adjudicate_simulator(sessions, adapter, "a", incident_id, effect_id) == "VERIFIED"
    assert await adjudicate_simulator(sessions, adapter, "a", incident_id, effect_id) == "VERIFIED"
    async with sessions() as session:
        outcomes = list((await session.scalars(select(OutcomeRevision))).all())
        assert len(outcomes) == 1
        first = outcomes[0]
    async with sessions() as session:
        disputed = await dispute_outcome(session, "a", first.id, 1, "independent check disagrees")
        assert disputed.label == "DISPUTED" and disputed.revision == 2
    assert await adjudicate_simulator(sessions, adapter, "a", incident_id, effect_id) == "DISPUTED"
    async with sessions() as session:
        assert len(list((await session.scalars(select(OutcomeRevision))).all())) == 2


@pytest.mark.asyncio
async def test_stale_revision_and_tenant_scope(witnessed_effect):
    sessions, adapter, incident_id, effect_id = witnessed_effect
    await adjudicate_simulator(sessions, adapter, "a", incident_id, effect_id)
    async with sessions() as session:
        first = await session.scalar(select(OutcomeRevision))
    async with sessions() as session:
        assert await dispute_outcome(session, "b", first.id, 1, "tenant mismatch") is None
    async with sessions() as session:
        with pytest.raises(OutcomeConflict):
            await dispute_outcome(session, "a", first.id, 5, "stale revision")
