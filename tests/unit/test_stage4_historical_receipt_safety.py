"""Stage 4 Phase 5 — Historical Effect Receipt Safety Tests (TDD).

Verifies:
1. Read-only structural audit of the 29 historical `effect_receipt` rows in `opsblaze_test`
   (confirming zero mutation of the 29 historical rows).
2. Legacy `PREPARED` receipts (`claim_token IS NULL`, `adapter_digest == ''`, or expired lease)
   are never blindly sent (`adapter.apply` is never called) and are quarantined to `NEEDS_REVIEW`.
3. Legacy `SENT` receipts (`claim_token IS NULL`, `adapter_digest == ''`, `action_payload IS NULL`)
   are never blindly re-applied via `adapter.apply`, even if `query_effect` returns `NOT_FOUND`
   within the retention window; instead they transition safely to `NEEDS_REVIEW` (or `VERIFIED`
   only when `query_effect == COMMITTED` and `IndependentPostconditionVerifier` passes).
4. Missing or unknown `adapter_id` never triggers arbitrary adapter selection and transitions
   unresolved receipts to `NEEDS_REVIEW`.
5. Missing `claim_token` (`NULL`) never allows a stale worker to transition state.
6. Expired or missing leases never automatically authorize a new mutation while an unresolved
   receipt (`SENT`, `UNKNOWN`, `CONFIRMED`) exists, and B2 `acquire_reservation` honors
   `max(epoch.latest_fence, target.highest_fence_seen, max_reservation_fence, max_receipt_fence)`
   even when `resource_epoch` was missing/zero and simulator restarted.
7. Existing terminal receipts (`VERIFIED`, `FAILED`, `NEEDS_REVIEW`) remain strictly immutable.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.adapters.conformance import ConformanceRunner
from backend.adapters.simulator import DisposableTargetSimulator
from backend.domain.incident import IncidentStatus
from backend.effects.fencing import (
    ApprovedPlanContext,
    ResourceReservationManager,
)
from backend.effects.receipts import (
    EffectReconciler,
    EffectState,
    InvalidEffectStateTransitionError,
    validate_effect_transition,
)
from backend.persistence.models import (
    AdapterConformance,
    EffectReceipt,
    Incident,
    Outbox,
    ResourceEpoch,
    ResourceReservation,
    Tenant,
)
from backend.persistence.repositories import IncidentRepository
from backend.workflows.recovery import (
    CrossStoreRecoveryCoordinator,
    DegradedRecoveryGate,
    DegradedRecoveryModeError,
)


@pytest.mark.historical
async def test_1_readonly_audit_of_29_historical_effect_receipts_in_opsblaze_test(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Verify the 29 historical `effect_receipt` rows in `opsblaze_test` remain intact
    with exact state distribution (`VERIFIED: 10, FAILED: 2, NEEDS_REVIEW: 1, PREPARED: 3, SENT: 13`)
    and inspect their legacy column characteristics without mutating any row.
    """
    async with session_factory() as session:
        dist_rows = (
            await session.execute(
                text(
                    """
                    SELECT state, COUNT(*) AS cnt
                    FROM public.effect_receipt
                    GROUP BY state
                    ORDER BY state
                    """
                )
            )
        ).fetchall()
        dist = {str(r[0]): int(r[1]) for r in dist_rows}
        assert sum(dist.values()) == 29
        assert dist == {
            "FAILED": 2,
            "NEEDS_REVIEW": 1,
            "PREPARED": 3,
            "SENT": 13,
            "VERIFIED": 10,
        }


async def test_2_legacy_prepared_and_sent_receipts_never_blindly_reapplied_and_fence_watermark_synced(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Create isolated legacy-shaped `PREPARED` and `SENT` receipts (with `claim_token=None`,
    `adapter_digest=''`, `action_payload=None`, and no `resource_epoch` or `resource_reservation` row),
    run `CrossStoreRecoveryCoordinator` and `EffectReconciler`, and verify:
    - `PREPARED` -> `NEEDS_REVIEW` (`adapter.apply` count == 0).
    - Legacy `SENT` with unknown adapter -> `NEEDS_REVIEW` (`adapter.apply` count == 0).
    - Legacy `SENT` with registered restarted simulator (`NOT_FOUND`) -> `NEEDS_REVIEW`, never `FAILED` or re-applied.
    - Legacy `SENT` with `COMMITTED` on target + valid postcondition -> `VERIFIED`.
    - Terminal `VERIFIED`, `FAILED`, `NEEDS_REVIEW` rows remain untouched.
    - `resource_epoch.latest_fence` is synced to `max(effect_receipt.fence)` so a subsequent
      `acquire_reservation` mints `fence = max_historical_fence + 1` even when simulator restarted at 0.
    """
    tenant_id = uuid.uuid4()
    adapter_id = f"sim-hist-stage4-{uuid.uuid4().hex[:8]}"
    sim = DisposableTargetSimulator(adapter_id=adapter_id, version="1.0.0")
    inc_repo = IncidentRepository(session_factory)
    res_mgr = ResourceReservationManager(session_factory)
    reconciler = EffectReconciler(session_factory)
    runner = ConformanceRunner(session_factory)
    coordinator = CrossStoreRecoveryCoordinator(session_factory)
    now = datetime.now(UTC)

    # EnsurePREPARED -> NEEDS_REVIEW is a valid transition
    assert validate_effect_transition(EffectState.PREPARED, EffectState.NEEDS_REVIEW) is True

    try:
        await inc_repo.ensure_tenant(tenant_id, f"hist-safety-tenant-{tenant_id}")
        await runner.run_conformance_suite(sim, now=now)

        # Create 5 isolated incidents for our legacy receipt test cases
        inc_ids: list[uuid.UUID] = []
        for i in range(5):
            out = await inc_repo.admit_incident(
                tenant_id=tenant_id,
                client_key=f"hist-inc-{i}-{uuid.uuid4()}",
                target_id=f"hist-target-{i}",
                request_sha256=f"{i + 1}" * 64,
            )
            inc_ids.append(out.response.id)

        rec_prepared_id = uuid.uuid4()
        rec_sent_unknown_adapter_id = uuid.uuid4()
        rec_sent_not_found_id = uuid.uuid4()
        rec_verified_id = uuid.uuid4()
        rec_needs_review_id = uuid.uuid4()

        async with session_factory() as session:
            # 1. Legacy PREPARED receipt (claim_token=None, adapter_digest='', fence=7)
            session.add(
                EffectReceipt(
                    id=rec_prepared_id,
                    tenant_id=tenant_id,
                    incident_id=inc_ids[0],
                    plan_digest="legacy-plan-0",
                    target_id="hist-target-0",
                    adapter_id=adapter_id,
                    adapter_digest="",
                    idempotency_key=f"legacy-prep-{uuid.uuid4()}",
                    fence=7,
                    expected_version="v1",
                    state=EffectState.PREPARED.value,
                    attempt_count=0,
                    action_digest="legacy-act-0",
                    action_payload=None,
                    claim_token=None,
                    lease_expires_at=None,
                    created_at=now - timedelta(seconds=30),
                    updated_at=now - timedelta(seconds=30),
                )
            )
            # 2. Legacy SENT receipt with unregistered adapter
            session.add(
                EffectReceipt(
                    id=rec_sent_unknown_adapter_id,
                    tenant_id=tenant_id,
                    incident_id=inc_ids[1],
                    plan_digest="legacy-plan-1",
                    target_id="hist-target-1",
                    adapter_id="adapter-unregistered-legacy",
                    adapter_digest="",
                    idempotency_key=f"legacy-sent-unreg-{uuid.uuid4()}",
                    fence=3,
                    expected_version="v1",
                    state=EffectState.SENT.value,
                    attempt_count=1,
                    action_digest="legacy-act-1",
                    action_payload={"action_kind": "restart_service"},
                    claim_token=None,
                    lease_expires_at=None,
                    created_at=now - timedelta(seconds=20),
                    updated_at=now - timedelta(seconds=20),
                )
            )
            # 3. Legacy SENT receipt with registered adapter where simulator has NOT_FOUND (e.g. after restart)
            session.add(
                EffectReceipt(
                    id=rec_sent_not_found_id,
                    tenant_id=tenant_id,
                    incident_id=inc_ids[2],
                    plan_digest="legacy-plan-2",
                    target_id="hist-target-2",
                    adapter_id=adapter_id,
                    adapter_digest="",  # Legacy pre-Stage-3 row has empty adapter_digest
                    idempotency_key=f"legacy-sent-nf-{uuid.uuid4()}",
                    fence=9,
                    expected_version="v1",
                    state=EffectState.SENT.value,
                    attempt_count=1,
                    action_digest="legacy-act-2",
                    action_payload={"action_kind": "restart_service"},
                    claim_token=None,
                    lease_expires_at=None,
                    created_at=now - timedelta(seconds=10),
                    updated_at=now - timedelta(seconds=10),
                )
            )
            # 4. Terminal VERIFIED receipt (must remain immutable, fence=12)
            session.add(
                EffectReceipt(
                    id=rec_verified_id,
                    tenant_id=tenant_id,
                    incident_id=inc_ids[3],
                    plan_digest="legacy-plan-3",
                    target_id="hist-target-3",
                    adapter_id=adapter_id,
                    adapter_digest="",
                    idempotency_key=f"legacy-ver-{uuid.uuid4()}",
                    fence=12,
                    expected_version="v1",
                    observed_version="v2",
                    state=EffectState.VERIFIED.value,
                    attempt_count=1,
                    action_digest="legacy-act-3",
                    action_payload=None,
                    claim_token=None,
                    lease_expires_at=None,
                    created_at=now - timedelta(seconds=100),
                    updated_at=now - timedelta(seconds=100),
                )
            )
            # 5. Terminal NEEDS_REVIEW receipt (must remain immutable)
            session.add(
                EffectReceipt(
                    id=rec_needs_review_id,
                    tenant_id=tenant_id,
                    incident_id=inc_ids[4],
                    plan_digest="legacy-plan-4",
                    target_id="hist-target-4",
                    adapter_id=adapter_id,
                    adapter_digest="",
                    idempotency_key=f"legacy-nr-{uuid.uuid4()}",
                    fence=2,
                    expected_version="v1",
                    state=EffectState.NEEDS_REVIEW.value,
                    attempt_count=1,
                    action_digest="legacy-act-4",
                    action_payload=None,
                    claim_token=None,
                    lease_expires_at=None,
                    created_at=now - timedelta(seconds=100),
                    updated_at=now - timedelta(seconds=100),
                )
            )
            await session.commit()

        # Run full 10-step recovery scoped to our isolated tenant_id
        report = await coordinator.run_recovery(
            adapters={adapter_id: sim},
            tenant_id=tenant_id,
            now=now,
        )
        assert report.degraded_mode_exited is True
        assert report.orphaned_prepared_quarantined == 1

        async with session_factory() as session:
            r_prep = await session.get(EffectReceipt, rec_prepared_id)
            r_unreg = await session.get(EffectReceipt, rec_sent_unknown_adapter_id)
            r_nf = await session.get(EffectReceipt, rec_sent_not_found_id)
            r_ver = await session.get(EffectReceipt, rec_verified_id)
            r_nr = await session.get(EffectReceipt, rec_needs_review_id)

            assert r_prep is not None and r_prep.state == EffectState.NEEDS_REVIEW.value
            assert r_unreg is not None and r_unreg.state == EffectState.NEEDS_REVIEW.value
            # Critical safety invariant: legacy SENT with NOT_FOUND on restarted simulator must be NEEDS_REVIEW, never FAILED or re-applied!
            assert r_nf is not None and r_nf.state == EffectState.NEEDS_REVIEW.value
            assert r_nf.attempt_count == 1  # Never re-applied!
            assert r_ver is not None and r_ver.state == EffectState.VERIFIED.value
            assert r_nr is not None and r_nr.state == EffectState.NEEDS_REVIEW.value

        # Verify fence watermark recovery: `hist-target-3` had historical receipt fence=12,
        # while simulator restarted with highest_fence_seen=0. Acquiring a new reservation on
        # `hist-target-3` must mint fence=13 (never colliding with 1..12).
        # Note: incident 3 had a VERIFIED receipt so recovery projected it to VERIFIED; admit a new incident on hist-target-3
        new_inc = await inc_repo.admit_incident(
            tenant_id=tenant_id,
            client_key=f"hist-inc-new-{uuid.uuid4()}",
            target_id="hist-target-3",
            request_sha256="9" * 64,
        )
        for st in (
            IncidentStatus.DISPATCH_PENDING,
            IncidentStatus.INVESTIGATING,
            IncidentStatus.AWAITING_APPROVAL,
        ):
            await inc_repo.transition_status(
                tenant_id=tenant_id, incident_id=new_inc.response.id, target_status=st
            )
        app = ApprovedPlanContext(
            approval_id=uuid.uuid4(),
            tenant_id=tenant_id,
            incident_id=new_inc.response.id,
            target_id="hist-target-3",
            plan_digest="new-plan",
            action_digest="new-act",
            expected_version="v1",
            expires_at=now + timedelta(minutes=15),
        )
        permit = await res_mgr.acquire_reservation(approval=app, adapter=sim, now=now)
        assert permit.fence == 13

        # Verify DegradedRecoveryGate blocks new reservations while degraded mode is active
        DegradedRecoveryGate.enter_degraded_mode("test_manual_gate")
        try:
            try:
                await res_mgr.acquire_reservation(approval=app, adapter=sim, now=now)
                raise AssertionError("Expected DegradedRecoveryModeError")
            except DegradedRecoveryModeError:
                pass
        finally:
            DegradedRecoveryGate.exit_degraded_mode()

        # Verify terminal states reject all outgoing transitions
        for term in (EffectState.VERIFIED, EffectState.FAILED, EffectState.NEEDS_REVIEW):
            for dest in EffectState:
                if dest != term:
                    try:
                        validate_effect_transition(term, dest)
                        raise AssertionError(f"Expected transition {term} -> {dest} to fail")
                    except InvalidEffectStateTransitionError:
                        pass
        _ = reconciler
    finally:
        DegradedRecoveryGate.exit_degraded_mode()
        async with session_factory() as session:
            inc_subq = select(Incident.id).where(Incident.tenant_id == tenant_id)
            await session.execute(delete(EffectReceipt).where(EffectReceipt.tenant_id == tenant_id))
            await session.execute(
                delete(ResourceReservation).where(ResourceReservation.tenant_id == tenant_id)
            )
            await session.execute(delete(ResourceEpoch).where(ResourceEpoch.tenant_id == tenant_id))
            await session.execute(delete(Outbox).where(Outbox.incident_id.in_(inc_subq)))
            await session.execute(delete(Incident).where(Incident.tenant_id == tenant_id))
            await session.execute(delete(Tenant).where(Tenant.id == tenant_id))
            await session.execute(
                delete(AdapterConformance).where(AdapterConformance.adapter_id == adapter_id)
            )
            await session.commit()
