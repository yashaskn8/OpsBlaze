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


@pytest.mark.asyncio
async def test_production_gateway_rejects_missing_approval_plan(lab, monkeypatch):
    from backend.effects.gateway import set_state
    from backend.settings import Settings

    sessions, incident_id, adapter = lab
    permit = await reserve_simulator_target(
        sessions, "a", "checkout", incident_id, "a" * 64
    )
    effect_id = await prepare_receipt(sessions, permit)
    monkeypatch.setattr(
        "backend.settings.get_settings",
        lambda: Settings(environment="production"),
    )
    with pytest.raises(EffectConflict, match="approved plan ID"):
        await execute_simulator_effect(sessions, adapter, permit)
    with pytest.raises(EffectConflict, match="approved plan ID"):
        await set_state(sessions, effect_id, "SENT")

    async with sessions() as session:
        receipt = await session.get(EffectReceipt, effect_id)
        target = await session.get(SimulatorTarget, ("a", "checkout"))
        assert receipt.state == "PREPARED"
        assert receipt.attempt_count == 0
        assert target.healthy is False


@pytest.mark.asyncio
async def test_lost_ack_with_unavailable_status_store_never_reapplies(lab):
    sessions, incident_id, adapter = lab
    permit = await reserve_simulator_target(
        sessions, "a", "checkout", incident_id, "7" * 64,
    )

    class BrokenLookupAfterCommit(SimulatorAdapter):
        async def apply(self, effect_id, permit):
            await super().apply(effect_id, permit)
            raise TimeoutError("reply lost")

        async def query_effect(self, effect_id):
            raise ConnectionError("status service offline")

    from backend.effects.gateway import reconcile_effect

    broken = BrokenLookupAfterCommit(sessions)
    assert await execute_simulator_effect(sessions, broken, permit) == "UNKNOWN"
    effect_id = (await find_receipt(sessions)).id
    assert await reconcile_effect(sessions, broken, effect_id) == "UNKNOWN"
    async with sessions() as session:
        receipt = await session.get(EffectReceipt, effect_id)
        assert receipt.state == "UNKNOWN" and receipt.attempt_count == 1
        applied = (await session.scalars(select(SimulatorAppliedEffect))).all()
        assert len(applied) == 1
    assert await adapter.observe("a", "checkout") == (2, True)
    assert await reconcile_effect(sessions, adapter, effect_id) == "VERIFIED"


@pytest.mark.asyncio
async def test_postcondition_timeout_keeps_receipt_unverified(lab):
    sessions, incident_id, adapter = lab
    permit = await reserve_simulator_target(
        sessions, "a", "checkout", incident_id, "8" * 64,
    )

    class LostOracle(SimulatorAdapter):
        async def observe(self, tenant_id, target_id):
            raise ConnectionError("oracle offline")

    from backend.effects.gateway import reconcile_effect

    assert await execute_simulator_effect(
        sessions, LostOracle(sessions), permit,
    ) == "UNKNOWN"
    effect_id = (await find_receipt(sessions)).id
    async with sessions() as session:
        receipt = await session.get(EffectReceipt, effect_id)
        assert receipt.state == "CONFIRMED"
    assert await reconcile_effect(sessions, adapter, effect_id) == "VERIFIED"


async def find_receipt(sessions):
    async with sessions() as session:
        return await session.scalar(select(EffectReceipt))


@pytest.mark.asyncio
async def test_parallel_sqlite_prepare_reuses_exact_immutable_effect_id(lab):
    import asyncio
    from datetime import datetime, timedelta, timezone

    sessions, incident_id, adapter = lab
    permit = Permit(
        tenant_id="a", incident_id=incident_id,
        target_id="checkout", action_digest="f" * 64,
        fence=1, expected_version=1,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    ids = await asyncio.gather(*(
        prepare_receipt(sessions, permit) for _ in range(5)
    ))
    assert len(set(ids)) == 1
    async with sessions() as session:
        receipts = (await session.scalars(select(EffectReceipt))).all()
        assert len(receipts) == 1 and receipts[0].attempt_count == 0



@pytest.mark.asyncio
async def test_sent_transition_may_only_be_claimed_once(lab):
    from backend.effects.gateway import set_state

    sessions, incident_id, adapter = lab
    permit = await reserve_simulator_target(
        sessions, "a", "checkout", incident_id, "9" * 64,
    )
    effect_id = await prepare_receipt(sessions, permit)
    await set_state(sessions, effect_id, "SENT")
    with pytest.raises(EffectConflict, match="already dispatched"):
        await set_state(sessions, effect_id, "SENT")
    async with sessions() as session:
        receipt = await session.get(EffectReceipt, effect_id)
        assert receipt.state == "SENT"
        assert receipt.attempt_count == 1
    assert await adapter.observe("a", "checkout") == (1, False)


@pytest.mark.asyncio
@pytest.mark.parametrize("starting_state", ["PREPARED", "SENT", "UNKNOWN"])
async def test_redteam_impossible_remote_status_never_confirms_effect(lab, starting_state):
    """No dispatch, retention gap or status jump may invent a verified effect."""
    from backend.effects.gateway import set_state

    sessions, incident_id, adapter = lab
    permit = await reserve_simulator_target(
        sessions, "a", "checkout", incident_id, "3" * 64,
    )
    effect_id = await prepare_receipt(sessions, permit)
    if starting_state == "SENT":
        await set_state(sessions, effect_id, "SENT")
    elif starting_state == "UNKNOWN":
        await set_state(sessions, effect_id, "UNKNOWN")

    class ContradictoryRemoteStatus(SimulatorAdapter):
        async def query_effect(self, _effect_id):
            return permit.expected_version + 50

    assert await reconcile_effect(
        sessions, ContradictoryRemoteStatus(sessions), effect_id
    ) == "NEEDS_REVIEW"
    async with sessions() as session:
        receipt = await session.get(EffectReceipt, effect_id)
        assert receipt.state == "NEEDS_REVIEW"
        assert receipt.observed_version is None
        target = await session.get(SimulatorTarget, ("a", "checkout"))
        assert target.version == permit.expected_version and not target.healthy
        assert (await session.scalars(select(SimulatorAppliedEffect))).all() == []


@pytest.mark.asyncio
async def test_redteam_wrong_confirmed_version_cannot_be_verified(lab):
    from backend.effects.gateway import set_state

    sessions, incident_id, adapter = lab
    permit = await reserve_simulator_target(
        sessions, "a", "checkout", incident_id, "4" * 64,
    )
    effect_id = await prepare_receipt(sessions, permit)
    await set_state(sessions, effect_id, "SENT")
    with pytest.raises(EffectConflict, match="confirmed effect version"):
        await set_state(
            sessions, effect_id, "CONFIRMED", permit.expected_version + 7,
        )
    await set_state(sessions, effect_id, "CONFIRMED", permit.expected_version + 1)
    # A direct stored-receipt edit must still be detected on reconciliation.
    async with sessions() as session:
        async with session.begin():
            forged_receipt = await session.get(EffectReceipt, effect_id)
            forged_receipt.observed_version = permit.expected_version + 7

    class FalseWitness(SimulatorAdapter):
        async def observe(self, tenant_id, target_id):
            return (permit.expected_version + 7, True)

    assert await reconcile_effect(
        sessions, FalseWitness(sessions), effect_id
    ) == "NEEDS_REVIEW"
    async with sessions() as session:
        receipt = await session.get(EffectReceipt, effect_id)
        assert receipt.state == "NEEDS_REVIEW"
        assert (await session.scalars(select(SimulatorAppliedEffect))).all() == []


@pytest.mark.asyncio
async def test_redteam_modified_permit_cannot_bypass_reserved_target_version(lab):
    sessions, incident_id, adapter = lab
    original = await reserve_simulator_target(
        sessions, "a", "checkout", incident_id, "5" * 64,
    )
    async with sessions() as session:
        async with session.begin():
            target = await session.get(SimulatorTarget, ("a", "checkout"))
            target.version += 1

    # An attacker-controlled permit cannot silently follow the external edit:
    # it must remain bound to the version in the durable reservation.
    forged = Permit(**{
        **original.__dict__,
        "expected_version": original.expected_version + 1,
    })
    with pytest.raises(TargetVersionConflict):
        await adapter.apply(uuid4(), forged)
    async with sessions() as session:
        target = await session.get(SimulatorTarget, ("a", "checkout"))
        assert target.version == 2 and not target.healthy
        assert (await session.scalars(select(SimulatorAppliedEffect))).all() == []


@pytest.mark.asyncio
async def test_redteam_verified_receipt_requires_matching_confirmed_version(lab):
    from backend.effects.gateway import set_state

    sessions, incident_id, adapter = lab
    permit = await reserve_simulator_target(
        sessions, "a", "checkout", incident_id, "8" * 64,
    )
    effect_id = await prepare_receipt(sessions, permit)
    await set_state(sessions, effect_id, "SENT")
    with pytest.raises(EffectConflict, match="confirmed effect version"):
        await set_state(sessions, effect_id, "CONFIRMED", True)
    await set_state(sessions, effect_id, "CONFIRMED", permit.expected_version + 1)
    async with sessions() as session:
        async with session.begin():
            receipt = await session.get(EffectReceipt, effect_id)
            receipt.observed_version = permit.expected_version + 99
    with pytest.raises(EffectConflict, match="cannot verify"):
        await set_state(sessions, effect_id, "VERIFIED")
    async with sessions() as session:
        receipt = await session.get(EffectReceipt, effect_id)
        assert receipt.state == "CONFIRMED"


@pytest.mark.asyncio
async def test_redteam_never_sent_receipt_cannot_acquire_verified_status(lab):
    from backend.effects.gateway import set_state

    sessions, incident_id, adapter = lab
    permit = await reserve_simulator_target(
        sessions, "a", "checkout", incident_id, "9" * 64,
    )
    effect_id = await prepare_receipt(sessions, permit)

    # Querying an unsent receipt does not fabricate an unknown external effect.
    assert await reconcile_effect(sessions, adapter, effect_id) == "PREPARED"
    async with sessions() as session:
        receipt = await session.get(EffectReceipt, effect_id)
        assert receipt.attempt_count == 0 and receipt.state == "PREPARED"

    # Even a matching forged remote identity and positive oracle cannot
    # upgrade UNKNOWN when no durable SEND has ever occurred.
    await set_state(sessions, effect_id, "UNKNOWN")

    class ForgedRemoteStatus(SimulatorAdapter):
        async def query_effect(self, effect_id):
            return permit.expected_version + 1

        async def observe(self, tenant_id, target_id):
            return permit.expected_version + 1, True

    assert await reconcile_effect(
        sessions, ForgedRemoteStatus(sessions), effect_id,
    ) == "NEEDS_REVIEW"
    async with sessions() as session:
        receipt = await session.get(EffectReceipt, effect_id)
        assert receipt.state == "NEEDS_REVIEW" and receipt.attempt_count == 0
        target = await session.get(SimulatorTarget, ("a", "checkout"))
        assert target.version == permit.expected_version and not target.healthy


@pytest.mark.asyncio
async def test_redteam_corrupt_confirmation_without_send_is_quarantined(lab):
    sessions, incident_id, adapter = lab
    permit = await reserve_simulator_target(
        sessions, "a", "checkout", incident_id, "a" * 64,
    )
    effect_id = await prepare_receipt(sessions, permit)
    async with sessions() as session:
        async with session.begin():
            receipt = await session.get(EffectReceipt, effect_id)
            receipt.state = "CONFIRMED"
            receipt.observed_version = permit.expected_version + 1
    assert await reconcile_effect(sessions, adapter, effect_id) == "NEEDS_REVIEW"
    async with sessions() as session:
        receipt = await session.get(EffectReceipt, effect_id)
        assert receipt.state == "NEEDS_REVIEW"


@pytest.mark.asyncio
@pytest.mark.parametrize("fake", [
    (2.0, True),
    (2, 1),
    ("2", True),
    (2,),
])
async def test_redteam_untyped_oracle_cannot_verify_effect(lab, fake):
    sessions, incident_id, adapter = lab
    permit = await reserve_simulator_target(
        sessions, "a", "checkout", incident_id, "b" * 64,
    )

    class UntypedWitness(SimulatorAdapter):
        async def observe(self, tenant_id, target_id):
            return fake

    result = await execute_simulator_effect(
        sessions, UntypedWitness(sessions), permit,
    )
    assert result == "NEEDS_REVIEW"
    async with sessions() as session:
        receipt = await session.scalar(select(EffectReceipt))
        assert receipt.state == "NEEDS_REVIEW"
        assert receipt.attempt_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["version", "send_count", "adapter"])
async def test_redteam_terminal_receipt_integrity_checked_on_replay(lab, tamper):
    sessions, incident_id, adapter = lab
    permit = await reserve_simulator_target(
        sessions, "a", "checkout", incident_id, "f" * 64,
    )
    assert await execute_simulator_effect(sessions, adapter, permit) == "VERIFIED"
    async with sessions() as session:
        receipt = await session.scalar(select(EffectReceipt))
        receipt_id = receipt.id

    async with sessions() as session:
        async with session.begin():
            receipt = await session.get(EffectReceipt, receipt_id)
            if tamper == "version":
                receipt.observed_version += 7
            elif tamper == "send_count":
                receipt.attempt_count = 0
            else:
                receipt.adapter_id = "untrusted-external-adapter"
    assert await reconcile_effect(sessions, adapter, receipt_id) == "NEEDS_REVIEW"
    async with sessions() as session:
        receipt = await session.get(EffectReceipt, receipt_id)
        assert receipt.state == "VERIFIED"  # preserved historical record


@pytest.mark.asyncio
async def test_receipt_pins_exact_original_permit_digest(lab):
    from backend.adapters.simulator import permit_digest

    sessions, incident_id, _adapter = lab
    permit = await reserve_simulator_target(
        sessions, "a", "checkout", incident_id, "1" * 64,
    )
    effect_id = await prepare_receipt(sessions, permit)
    async with sessions() as session:
        receipt = await session.get(EffectReceipt, effect_id)
        assert receipt.permit_digest == permit_digest(permit)
        assert len(receipt.permit_digest) == 64


@pytest.mark.asyncio
async def test_poisoned_effect_status_cannot_be_upgraded_to_verified(lab):
    """An applied effect for the wrong permit is not a valid recovery lookup."""
    sessions, incident_id, _adapter = lab
    permit = await reserve_simulator_target(
        sessions, "a", "checkout", incident_id, "2" * 64,
    )

    class LostAckAfterCommit(SimulatorAdapter):
        async def apply(self, effect_id, permit):
            await super().apply(effect_id, permit)
            raise TimeoutError("simulated lost acknowledgment")

    assert await execute_simulator_effect(
        sessions, LostAckAfterCommit(sessions), permit,
    ) == "UNKNOWN"
    async with sessions() as session:
        receipt = await session.scalar(select(EffectReceipt))
        effect_id = receipt.id
        assert receipt.attempt_count == 1
        assert receipt.permit_digest is not None
    async with sessions() as session:
        async with session.begin():
            applied = await session.get(SimulatorAppliedEffect, effect_id)
            assert applied is not None
            applied.permit_digest = "f" * 64

    assert await reconcile_effect(
        sessions, SimulatorAdapter(sessions), effect_id,
    ) == "NEEDS_REVIEW"
    async with sessions() as session:
        receipt = await session.get(EffectReceipt, effect_id)
        target = await session.get(SimulatorTarget, ("a", "checkout"))
        assert receipt.state == "NEEDS_REVIEW"
        assert receipt.observed_version is None
        assert target.healthy is True  # Applied once; not verified as our permit.


@pytest.mark.asyncio
async def test_legacy_effect_receipt_cannot_be_reused_for_dispatch(lab):
    sessions, incident_id, adapter = lab
    permit = await reserve_simulator_target(
        sessions, "a", "checkout", incident_id, "3" * 64,
    )
    effect_id = await prepare_receipt(sessions, permit)
    async with sessions() as session:
        async with session.begin():
            receipt = await session.get(EffectReceipt, effect_id)
            receipt.permit_digest = None
    with pytest.raises(EffectConflict, match="effect identity reused"):
        await execute_simulator_effect(sessions, adapter, permit)
    async with sessions() as session:
        receipt = await session.get(EffectReceipt, effect_id)
        assert receipt.state == "PREPARED"
        assert receipt.attempt_count == 0
        assert (await session.scalars(select(SimulatorAppliedEffect))).all() == []


@pytest.mark.asyncio
async def test_adapter_permit_identity_failure_is_manual_review(lab):
    sessions, incident_id, _adapter = lab
    permit = await reserve_simulator_target(
        sessions, "a", "checkout", incident_id, "4" * 64,
    )

    class ForgedApply(SimulatorAdapter):
        async def apply(self, effect_id, permit):
            raise EffectIdentityConflict("effect identity mismatch")

    assert await execute_simulator_effect(
        sessions, ForgedApply(sessions), permit,
    ) == "NEEDS_REVIEW"
    async with sessions() as session:
        receipt = await session.scalar(select(EffectReceipt))
        assert receipt.state == "NEEDS_REVIEW"
        assert receipt.attempt_count == 1
        assert (await session.scalars(select(SimulatorAppliedEffect))).all() == []


@pytest.mark.asyncio
async def test_confirmed_effect_rechecks_permit_before_oracle_verification(lab):
    sessions, incident_id, _adapter = lab
    permit = await reserve_simulator_target(
        sessions, "a", "checkout", incident_id, "5" * 64,
    )

    class UnavailableWitness(SimulatorAdapter):
        async def observe(self, tenant_id, target_id):
            raise ConnectionError("independent oracle unavailable")

    assert await execute_simulator_effect(
        sessions, UnavailableWitness(sessions), permit,
    ) == "UNKNOWN"
    async with sessions() as session:
        receipt = await session.scalar(select(EffectReceipt))
        effect_id = receipt.id
        assert receipt.state == "CONFIRMED" and receipt.attempt_count == 1

    async with sessions() as session:
        async with session.begin():
            row = await session.get(SimulatorAppliedEffect, effect_id)
            row.permit_digest = "0" * 64

    assert await reconcile_effect(
        sessions, SimulatorAdapter(sessions), effect_id,
    ) == "NEEDS_REVIEW"
    async with sessions() as session:
        assert (await session.get(EffectReceipt, effect_id)).state == "NEEDS_REVIEW"


@pytest.mark.asyncio
async def test_confirmed_effect_missing_status_remains_unverified(lab):
    sessions, incident_id, _adapter = lab
    permit = await reserve_simulator_target(
        sessions, "a", "checkout", incident_id, "6" * 64,
    )

    class UnavailableWitness(SimulatorAdapter):
        async def observe(self, tenant_id, target_id):
            raise TimeoutError("independent oracle unavailable")

    assert await execute_simulator_effect(
        sessions, UnavailableWitness(sessions), permit,
    ) == "UNKNOWN"
    async with sessions() as session:
        effect_id = (await session.scalar(select(EffectReceipt))).id

    class ForgottenStatus(SimulatorAdapter):
        async def query_effect(self, effect_id):
            return None

    assert await reconcile_effect(
        sessions, ForgottenStatus(sessions), effect_id,
    ) == "UNKNOWN"
    async with sessions() as session:
        row = await session.get(EffectReceipt, effect_id)
        assert row.state == "CONFIRMED" and row.attempt_count == 1


@pytest.mark.asyncio
async def test_direct_dispatch_refuses_legacy_receipt_without_permit_binding(lab):
    from backend.effects.gateway import set_state

    sessions, incident_id, _adapter = lab
    permit = await reserve_simulator_target(
        sessions, "a", "checkout", incident_id, "7" * 64,
    )
    effect_id = await prepare_receipt(sessions, permit)
    async with sessions() as session:
        async with session.begin():
            row = await session.get(EffectReceipt, effect_id)
            row.permit_digest = None

    with pytest.raises(EffectConflict, match="permit binding"):
        await set_state(sessions, effect_id, "SENT")
    async with sessions() as session:
        row = await session.get(EffectReceipt, effect_id)
        assert row.state == "PREPARED" and row.attempt_count == 0


@pytest.mark.asyncio
async def test_redteam_reservation_status_read_view_lifecycle_and_contention(lab):
    """B2-05 & B2-06: authorized reservation status view, explicit release/expiry & contention."""
    import asyncio
    import time
    from httpx import ASGITransport, AsyncClient

    from backend.api import app
    from backend.database import get_session
    from backend.effects.reservations import (
        expire_stale_reservations,
        list_reservations,
        read_reservation_status,
        release_simulator_reservation,
    )
    from backend.models import ResourceReservation
    from backend.settings import Settings, get_settings

    sessions, incident_id, adapter = lab
    # Seed a foreign tenant target to verify strict tenant isolation.
    async with sessions() as session:
        async with session.begin():
            session.add(SimulatorTarget(tenant_id="b", target_id="secret-db", version=1, healthy=False, last_fence=0))
            session.add(ResourceEpoch(tenant_id="b", target_id="secret-db", latest_fence=0, version_seen=1))
    async with sessions() as session:
        foreign_inc = await admit_incident(session, "b", "foreign-res", IncidentCreate(target_id="secret-db"))
    await reserve_simulator_target(sessions, "b", "secret-db", foreign_inc.id, "f" * 64)

    # Measure bounded concurrent contention across 8 competing incidents on the same target.
    competitors = [uuid4() for _ in range(8)]
    t0 = time.perf_counter()
    results = await asyncio.gather(
        *(
            reserve_simulator_target(sessions, "a", "checkout", cid, f"{i:064x}")
            for i, cid in enumerate(competitors)
        ),
        return_exceptions=True,
    )
    contention_ms = (time.perf_counter() - t0) * 1000
    winners = [r for r in results if isinstance(r, Permit)]
    busy = [r for r in results if isinstance(r, ReservationBusy)]
    assert len(winners) == 1
    assert len(busy) == 7
    assert contention_ms < 5000
    winner = winners[0]

    # Verify authorized read API and strict cross-tenant isolation.
    async def override_session():
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: Settings(local_token="res-token")
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            own = {"X-Tenant-Id": "a", "X-OpsBlaze-Token": "res-token"}
            unauth = {"X-Tenant-Id": "a", "X-OpsBlaze-Token": "wrong"}
            assert (await client.get("/v1/reservations", headers=unauth)).status_code == 401
            listing = await client.get("/v1/reservations", headers=own)
            assert listing.status_code == 200
            body = listing.json()
            assert len(body["reservations"]) == 1
            assert body["reservations"][0]["target_id"] == "checkout"
            assert body["reservations"][0]["effective_state"] == "RESERVED"
            assert body["reservations"][0]["fence"] == winner.fence
            assert "secret-db" not in listing.text

            single = await client.get("/v1/reservations/checkout", headers=own)
            assert single.status_code == 200
            assert single.json()["holder_incident_id"] == str(winner.incident_id)
            assert (await client.get("/v1/reservations/secret-db", headers=own)).status_code == 404
            assert (await client.get("/v1/reservations?limit=51", headers=own)).status_code == 422
    finally:
        app.dependency_overrides.clear()

    # Expired projection without mutating fence, followed by durable expiry sweep.
    future = winner.expires_at + timedelta(seconds=5)
    async with sessions() as session:
        projected = await read_reservation_status(session, "a", "checkout", now=future)
        assert projected["stored_state"] == "RESERVED"
        assert projected["effective_state"] == "EXPIRED"
        assert projected["expired"] is True
        assert projected["lease_remaining_seconds"] == 0
    assert await expire_stale_reservations(sessions, now=future) >= 1
    async with sessions() as session:
        row = await session.get(ResourceReservation, ("a", "checkout"))
        assert row.state == "EXPIRED"
        assert row.fence == winner.fence

    # New reservation gets a strictly higher monotonic fence, and explicit release marks RELEASED.
    second = await reserve_simulator_target(sessions, "a", "checkout", incident_id, "e" * 64)
    assert second.fence > winner.fence
    assert await release_simulator_reservation(sessions, "a", "checkout", incident_id, second.fence) is True
    async with sessions() as session:
        released = await read_reservation_status(session, "a", "checkout")
        assert released["stored_state"] == "RELEASED"
        assert released["effective_state"] == "RELEASED"
        assert released["latest_fence"] == second.fence
        listed = await list_reservations(session, "a")
        assert listed["reservations"][0]["effective_state"] == "RELEASED"
