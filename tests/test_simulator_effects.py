from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.adapters.simulator import (
    EffectIdentityConflict, Permit, SimulatorAdapter, StaleFence, TargetVersionConflict
)
from backend.effects.gateway import (
    EffectConflict, execute_simulator_effect, prepare_receipt, reconcile_effect
)
from backend.effects.reservations import ReservationBusy, reserve_simulator_target
from backend.models import EffectReceipt, ResourceEpoch, SimulatorAppliedEffect, SimulatorTarget
from backend.schemas import IncidentCreate
from backend.services import admit_incident


@pytest.fixture
async def lab(sessions_fixture):
    async with sessions_fixture() as session:
        async with session.begin():
            session.add(SimulatorTarget(tenant_id="a", target_id="checkout", version=1, healthy=False, last_fence=0))
            session.add(ResourceEpoch(tenant_id="a", target_id="checkout", latest_fence=0, version_seen=1))
    async with sessions_fixture() as session:
        incident = await admit_incident(session, "a", "id-1", IncidentCreate(target_id="checkout"))
    return sessions_fixture, incident.id, SimulatorAdapter(sessions_fixture)


@pytest.mark.asyncio
async def test_effect_receipt_and_independent_verification(lab):
    sessions, incident_id, adapter = lab
    permit = await reserve_simulator_target(sessions, "a", "checkout", incident_id, "a" * 64)
    assert await execute_simulator_effect(sessions, adapter, permit) == "VERIFIED"
    assert await execute_simulator_effect(sessions, adapter, permit) == "VERIFIED"
    async with sessions() as session:
        effects = list((await session.scalars(select(SimulatorAppliedEffect))).all())
        receipts = list((await session.scalars(select(EffectReceipt))).all())
        assert len(effects) == len(receipts) == 1
        assert receipts[0].attempt_count == 1
        assert effects[0].observed_version == 2


@pytest.mark.asyncio
async def test_expired_or_replaced_fence_is_rejected(lab):
    sessions, incident_id, adapter = lab
    original = await reserve_simulator_target(sessions, "a", "checkout", incident_id, "b" * 64)
    with pytest.raises(ReservationBusy):
        await reserve_simulator_target(sessions, "a", "checkout", incident_id, "c" * 64)
    async with sessions() as session:
        async with session.begin():
            from backend.models import ResourceReservation
            row = await session.get(ResourceReservation, ("a", "checkout"))
            row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    replacement = await reserve_simulator_target(sessions, "a", "checkout", incident_id, "c" * 64)
    assert replacement.fence > original.fence
    with pytest.raises(StaleFence):
        await adapter.apply(uuid4(), original)


@pytest.mark.asyncio
async def test_version_mismatch_does_not_apply(lab):
    sessions, incident_id, adapter = lab
    permit = await reserve_simulator_target(sessions, "a", "checkout", incident_id, "d" * 64)
    bad = Permit(**{**permit.__dict__, "expected_version": permit.expected_version + 1})
    with pytest.raises(TargetVersionConflict):
        await adapter.apply(uuid4(), bad)
    observed = await adapter.observe("a", "checkout")
    assert observed == (1, False)


@pytest.mark.asyncio
async def test_uncertain_response_is_reconciled_without_reapply(lab):
    sessions, incident_id, adapter = lab
    permit = await reserve_simulator_target(sessions, "a", "checkout", incident_id, "e" * 64)
    effect_id = await prepare_receipt(sessions, permit)

    class LostAckSimulator(SimulatorAdapter):
        async def apply(self, effect_id, permit):
            await super().apply(effect_id, permit)
            raise TimeoutError("commit succeeded but reply was lost")

    assert await execute_simulator_effect(sessions, LostAckSimulator(sessions), permit) == "UNKNOWN"
    assert await reconcile_effect(sessions, adapter, effect_id) == "VERIFIED"
    async with sessions() as session:
        applied = list((await session.scalars(select(SimulatorAppliedEffect))).all())
        assert len(applied) == 1


@pytest.mark.asyncio
async def test_conflicting_effect_identity_rejected(lab):
    sessions, incident_id, adapter = lab
    permit = await reserve_simulator_target(sessions, "a", "checkout", incident_id, "f" * 64)
    await prepare_receipt(sessions, permit)
    different = Permit(**{**permit.__dict__, "expected_version": 100})
    with pytest.raises(EffectConflict):
        await prepare_receipt(sessions, different)


@pytest.mark.asyncio
async def test_applied_effect_identity_rejects_different_permit(lab):
    sessions, incident_id, adapter = lab
    permit = await reserve_simulator_target(
        sessions, "a", "checkout", incident_id, "a" * 64
    )
    effect_id = uuid4()
    assert await adapter.apply(effect_id, permit) == 2
    changed = Permit(
        tenant_id=permit.tenant_id,
        target_id=permit.target_id,
        incident_id=permit.incident_id,
        action_digest="b" * 64,
        fence=permit.fence,
        expected_version=permit.expected_version,
        expires_at=permit.expires_at,
    )
    with pytest.raises(EffectIdentityConflict):
        await adapter.apply(effect_id, changed)
    assert await adapter.apply(effect_id, permit) == 2
    assert await adapter.observe("a", "checkout") == (2, True)
