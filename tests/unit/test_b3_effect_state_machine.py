"""Unit and integration tests for Feature B3 — Effect Receipt and Reconciliation."""

from __future__ import annotations

import random
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.adapters.conformance import (
    AdapterQuarantinedError,
    ConformanceRunner,
    ConformanceStatus,
    MutationCapabilityGate,
)
from backend.adapters.fault_proxy import (
    FaultInjectionProxy,
    FaultRule,
    FaultScenario,
)
from backend.adapters.simulator import DisposableTargetSimulator
from backend.domain.incident import IncidentStatus
from backend.effects.fencing import (
    ApprovedPlanContext,
    ResourceReservationManager,
)
from backend.effects.receipts import (
    ActionGateway,
    ActionSpec,
    EffectReconciler,
    EffectState,
    InvalidEffectStateTransitionError,
    compute_effect_idempotency_key,
    validate_effect_transition,
)
from backend.observability.metrics import metrics
from backend.persistence.models import AdapterConformance
from backend.persistence.repositories import IncidentRepository


@pytest.fixture
async def certified_b3_simulator(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[DisposableTargetSimulator]:
    adapter_id = f"sim-b3-{uuid.uuid4().hex[:8]}"
    sim = DisposableTargetSimulator(
        adapter_id=adapter_id,
        version="1.0.0",
        retention_horizon_seconds=120,
    )
    runner = ConformanceRunner(session_factory)
    report = await runner.run_conformance_suite(sim)
    assert report.passed is True
    try:
        yield sim
    finally:
        async with session_factory() as session:
            await session.execute(
                delete(AdapterConformance).where(AdapterConformance.adapter_id == adapter_id)
            )
            await session.commit()


async def _admit_incident_to_executing(
    session_factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    target_id: str,
) -> uuid.UUID:
    repo = IncidentRepository(session_factory)
    outcome = await repo.admit_incident(
        tenant_id=tenant_id,
        client_key=f"b3-inc-{uuid.uuid4()}",
        target_id=target_id,
        request_sha256="b" * 64,
    )
    inc_id = outcome.response.id
    for st in (
        IncidentStatus.DISPATCH_PENDING,
        IncidentStatus.INVESTIGATING,
        IncidentStatus.AWAITING_APPROVAL,
        IncidentStatus.EXECUTING,
    ):
        await repo.transition_status(tenant_id=tenant_id, incident_id=inc_id, target_status=st)
    return inc_id


def test_b3_effect_state_machine_idempotency_key_and_property_walks() -> None:
    """Verify B3 effect receipt state machine legal/illegal transitions and property walks."""
    assert validate_effect_transition(EffectState.PREPARED, EffectState.SENT)
    assert validate_effect_transition(EffectState.PREPARED, EffectState.FAILED)
    assert validate_effect_transition(EffectState.SENT, EffectState.CONFIRMED)
    assert validate_effect_transition(EffectState.SENT, EffectState.UNKNOWN)
    assert validate_effect_transition(EffectState.SENT, EffectState.FAILED)
    assert validate_effect_transition(EffectState.SENT, EffectState.NEEDS_REVIEW)
    assert validate_effect_transition(EffectState.UNKNOWN, EffectState.CONFIRMED)
    assert validate_effect_transition(EffectState.UNKNOWN, EffectState.FAILED)
    assert validate_effect_transition(EffectState.UNKNOWN, EffectState.NEEDS_REVIEW)
    # Controlled retry from UNKNOWN -> SENT is only permitted when allow_controlled_retry=True
    with pytest.raises(InvalidEffectStateTransitionError):
        validate_effect_transition(EffectState.UNKNOWN, EffectState.SENT)
    assert validate_effect_transition(
        EffectState.UNKNOWN, EffectState.SENT, allow_controlled_retry=True
    )
    assert validate_effect_transition(EffectState.CONFIRMED, EffectState.VERIFIED)
    assert validate_effect_transition(EffectState.CONFIRMED, EffectState.FAILED)
    assert validate_effect_transition(EffectState.CONFIRMED, EffectState.NEEDS_REVIEW)

    # Terminal states reject all outgoing transitions
    for term in (EffectState.VERIFIED, EffectState.FAILED, EffectState.NEEDS_REVIEW):
        for target in EffectState:
            if target != term:
                with pytest.raises(InvalidEffectStateTransitionError):
                    validate_effect_transition(term, target)

    # Cannot skip directly from PREPARED or SENT or UNKNOWN to VERIFIED without CONFIRMED + verifier
    for src in (EffectState.PREPARED, EffectState.SENT, EffectState.UNKNOWN):
        with pytest.raises(InvalidEffectStateTransitionError):
            validate_effect_transition(src, EffectState.VERIFIED)

    # Deterministic idempotency key invariance
    t_id = uuid.uuid4()
    inc_id = uuid.uuid4()
    k1 = compute_effect_idempotency_key(
        tenant_id=t_id,
        incident_id=inc_id,
        plan_digest="plan-1",
        target_id="target-1",
        adapter_id="sim-1",
        action_digest="act-1",
        expected_version="v1",
    )
    k2 = compute_effect_idempotency_key(
        tenant_id=t_id,
        incident_id=inc_id,
        plan_digest="plan-1",
        target_id="target-1",
        adapter_id="sim-1",
        action_digest="act-1",
        expected_version="v1",
    )
    assert k1 == k2 and len(k1) == 64

    # Property-based random walks over EffectState never bypass terminal immutability
    rng = random.Random(20261107)
    all_states = list(EffectState)
    for _ in range(250):
        cur = EffectState.PREPARED
        for _ in range(12):
            nxt = rng.choice(all_states)
            try:
                validate_effect_transition(cur, nxt)
                if cur in (EffectState.VERIFIED, EffectState.FAILED, EffectState.NEEDS_REVIEW):
                    assert nxt == cur
                cur = nxt
            except InvalidEffectStateTransitionError:
                pass


async def test_b3_happy_path_duplicate_dispatch_and_lost_response_reconciliation(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
    certified_b3_simulator: DisposableTargetSimulator,
) -> None:
    """Scenarios 1-6:
    - Happy path PREPARED -> SENT -> CONFIRMED -> VERIFIED with independent postcondition check.
    - Duplicate dispatch with same effect_id returns VERIFIED without second target mutation.
    - Lost response after commit -> UNKNOWN -> EffectReconciler queries adapter -> VERIFIED.
    """
    res_mgr = ResourceReservationManager(session_factory)
    gateway = ActionGateway(session_factory)
    reconciler = EffectReconciler(session_factory)

    # 1. Happy path + duplicate dispatch
    target_1 = "sim-b3-happy-target"
    inc_1 = await _admit_incident_to_executing(session_factory, isolated_tenant, target_1)
    action_1 = ActionSpec(
        action_kind="restart_service",
        parameters={"graceful": True},
    )
    now = datetime.now(UTC)
    approval_1 = ApprovedPlanContext(
        approval_id=uuid.uuid4(),
        tenant_id=isolated_tenant,
        incident_id=inc_1,
        target_id=target_1,
        plan_digest="plan-happy-1",
        action_digest=action_1.action_digest(),
        expected_version="v1",
        expires_at=now + timedelta(minutes=10),
    )
    permit_1 = await res_mgr.acquire_reservation(
        approval=approval_1, adapter=certified_b3_simulator
    )
    receipt_1 = await gateway.execute_with_permit(
        permit=permit_1,
        approval=approval_1,
        action=action_1,
        adapter=certified_b3_simulator,
    )
    assert receipt_1.state == EffectState.VERIFIED
    assert receipt_1.observed_version == "v2"
    snap_1 = await certified_b3_simulator.read_resource(isolated_tenant, target_1)
    assert snap_1.current_version == "v2"
    assert snap_1.state_data["mutation_count"] == 1

    # Duplicate dispatch of the same (permit_1, approval_1, action_1) returns the VERIFIED receipt
    # without calling apply a second time
    dup_receipt_1 = await gateway.execute_with_permit(
        permit=permit_1,
        approval=approval_1,
        action=action_1,
        adapter=certified_b3_simulator,
    )
    assert dup_receipt_1.id == receipt_1.id
    assert dup_receipt_1.state == EffectState.VERIFIED
    snap_1_after = await certified_b3_simulator.read_resource(isolated_tenant, target_1)
    assert snap_1_after.state_data["mutation_count"] == 1

    # 2. Lost response after commit -> UNKNOWN -> Reconciled to VERIFIED without duplicate mutation
    target_2 = "sim-b3-lost-resp-target"
    inc_2 = await _admit_incident_to_executing(session_factory, isolated_tenant, target_2)
    action_2 = ActionSpec(action_kind="drain_traffic", parameters={"timeout_s": 5})
    approval_2 = ApprovedPlanContext(
        approval_id=uuid.uuid4(),
        tenant_id=isolated_tenant,
        incident_id=inc_2,
        target_id=target_2,
        plan_digest="plan-lost-2",
        action_digest=action_2.action_digest(),
        expected_version="v1",
        expires_at=now + timedelta(minutes=10),
    )
    proxy = FaultInjectionProxy(certified_b3_simulator)
    permit_2 = await res_mgr.acquire_reservation(approval=approval_2, adapter=proxy)
    proxy.set_rule(
        FaultRule(
            scenario=FaultScenario.LOST_RESPONSE_AFTER_COMMIT,
            target_id=target_2,
            max_triggers=1,
        )
    )
    receipt_2_unknown = await gateway.execute_with_permit(
        permit=permit_2,
        approval=approval_2,
        action=action_2,
        adapter=proxy,
    )
    assert receipt_2_unknown.state == EffectState.UNKNOWN

    # Reconciler claims the UNKNOWN receipt, calls query_effect + read_resource, and resolves to VERIFIED
    reconciled = await reconciler.reconcile_pending(
        adapters={proxy.profile().adapter_id: proxy},
        tenant_id=isolated_tenant,
    )
    assert len(reconciled) == 1
    assert reconciled[0].id == receipt_2_unknown.id
    assert reconciled[0].state == EffectState.VERIFIED
    assert reconciled[0].observed_version == "v2"
    snap_2 = await certified_b3_simulator.read_resource(isolated_tenant, target_2)
    assert snap_2.current_version == "v2"
    assert snap_2.state_data["mutation_count"] == 1


async def test_b3_false_success_partial_effect_retention_expiry_and_lookup_outage(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    """Scenarios 7-13:
    - False success response caught by IndependentPostconditionVerifier -> NEEDS_REVIEW + adapter QUARANTINED.
    - Partial effect -> NEEDS_REVIEW.
    - Retention expiry during UNKNOWN -> NEEDS_REVIEW (no blind retry).
    - Status lookup outage exhausting max_reconcile_attempts -> NEEDS_REVIEW.
    """
    clock_now = datetime(2026, 11, 7, 18, 0, 0, tzinfo=UTC)

    def get_clock() -> datetime:
        return clock_now

    adapter_id = f"sim-b3-faults-{uuid.uuid4().hex[:8]}"
    sim = DisposableTargetSimulator(
        adapter_id=adapter_id,
        version="1.0.0",
        retention_horizon_seconds=60,
        now_fn=get_clock,
    )
    runner = ConformanceRunner(session_factory)
    gate = MutationCapabilityGate(session_factory)
    res_mgr = ResourceReservationManager(session_factory)
    gateway = ActionGateway(session_factory)
    reconciler = EffectReconciler(session_factory, max_reconcile_attempts=2)

    try:
        await runner.run_conformance_suite(sim, now=clock_now)
        proxy = FaultInjectionProxy(sim)

        # A. Partial effect -> NEEDS_REVIEW
        t_partial = "sim-target-partial"
        inc_p = await _admit_incident_to_executing(session_factory, isolated_tenant, t_partial)
        act_p = ActionSpec(action_kind="restart_service")
        app_p = ApprovedPlanContext(
            approval_id=uuid.uuid4(),
            tenant_id=isolated_tenant,
            incident_id=inc_p,
            target_id=t_partial,
            plan_digest="plan-partial",
            action_digest=act_p.action_digest(),
            expected_version="v1",
            expires_at=clock_now + timedelta(minutes=20),
        )
        perm_p = await res_mgr.acquire_reservation(approval=app_p, adapter=proxy, now=clock_now)
        proxy.set_rule(
            FaultRule(scenario=FaultScenario.PARTIAL_EFFECT, target_id=t_partial, max_triggers=1)
        )
        rec_p = await gateway.execute_with_permit(
            permit=perm_p, approval=app_p, action=act_p, adapter=proxy, now=clock_now
        )
        assert rec_p.state == EffectState.NEEDS_REVIEW

        # B. Retention expiry during UNKNOWN -> NEEDS_REVIEW (no blind retry)
        t_ret = "sim-target-retention"
        inc_r = await _admit_incident_to_executing(session_factory, isolated_tenant, t_ret)
        act_r = ActionSpec(action_kind="restart_service")
        app_r = ApprovedPlanContext(
            approval_id=uuid.uuid4(),
            tenant_id=isolated_tenant,
            incident_id=inc_r,
            target_id=t_ret,
            plan_digest="plan-ret",
            action_digest=act_r.action_digest(),
            expected_version="v1",
            expires_at=clock_now + timedelta(minutes=20),
        )
        perm_r = await res_mgr.acquire_reservation(approval=app_r, adapter=proxy, now=clock_now)
        proxy.clear_rules()
        proxy.set_rule(
            FaultRule(
                scenario=FaultScenario.LOST_RESPONSE_AFTER_COMMIT,
                target_id=t_ret,
                max_triggers=1,
            )
        )
        rec_r_unk = await gateway.execute_with_permit(
            permit=perm_r, approval=app_r, action=act_r, adapter=proxy, now=clock_now
        )
        assert rec_r_unk.state == EffectState.UNKNOWN

        # Advance clock past 60s retention horizon
        clock_now = clock_now + timedelta(seconds=90)
        reconciled_ret = await reconciler.reconcile_pending(
            adapters={adapter_id: proxy},
            tenant_id=isolated_tenant,
            now=clock_now,
        )
        assert len(reconciled_ret) == 1
        assert reconciled_ret[0].state == EffectState.NEEDS_REVIEW
        assert metrics.snapshot()["counters"]["adapter_retention_gaps_total"] >= 1

        # C. Status store outage exhausting max_reconcile_attempts=2 -> NEEDS_REVIEW
        t_out = "sim-target-outage"
        inc_o = await _admit_incident_to_executing(session_factory, isolated_tenant, t_out)
        act_o = ActionSpec(action_kind="restart_service")
        app_o = ApprovedPlanContext(
            approval_id=uuid.uuid4(),
            tenant_id=isolated_tenant,
            incident_id=inc_o,
            target_id=t_out,
            plan_digest="plan-out",
            action_digest=act_o.action_digest(),
            expected_version="v1",
            expires_at=clock_now + timedelta(minutes=20),
        )
        perm_o = await res_mgr.acquire_reservation(approval=app_o, adapter=proxy, now=clock_now)
        proxy.clear_rules()
        proxy.set_rule(
            FaultRule(
                scenario=FaultScenario.LOST_RESPONSE_AFTER_COMMIT,
                target_id=t_out,
                max_triggers=1,
            )
        )
        rec_o_unk = await gateway.execute_with_permit(
            permit=perm_o, approval=app_o, action=act_o, adapter=proxy, now=clock_now
        )
        assert rec_o_unk.state == EffectState.UNKNOWN

        proxy.set_rule(FaultRule(scenario=FaultScenario.STATUS_STORE_OUTAGE, target_id=t_out))
        # Attempt 1 keeps it UNKNOWN with backoff
        r_try1 = await reconciler.reconcile_pending(
            adapters={adapter_id: proxy},
            tenant_id=isolated_tenant,
            now=clock_now + timedelta(seconds=1),
        )
        assert len(r_try1) == 1
        assert r_try1[0].state == EffectState.UNKNOWN

        # Attempt 2 reaches max_reconcile_attempts=2 -> transitions to NEEDS_REVIEW
        r_try2 = await reconciler.reconcile_pending(
            adapters={adapter_id: proxy},
            tenant_id=isolated_tenant,
            now=clock_now + timedelta(seconds=20),
        )
        assert len(r_try2) == 1
        assert r_try2[0].state == EffectState.NEEDS_REVIEW

        # D. False success response -> caught by postcondition verifier -> NEEDS_REVIEW + QUARANTINED
        t_false = "sim-target-false-success"
        inc_f = await _admit_incident_to_executing(session_factory, isolated_tenant, t_false)
        act_f = ActionSpec(action_kind="restart_service")
        app_f = ApprovedPlanContext(
            approval_id=uuid.uuid4(),
            tenant_id=isolated_tenant,
            incident_id=inc_f,
            target_id=t_false,
            plan_digest="plan-false",
            action_digest=act_f.action_digest(),
            expected_version="v1",
            expires_at=clock_now + timedelta(minutes=20),
        )
        proxy.clear_rules()
        perm_f = await res_mgr.acquire_reservation(approval=app_f, adapter=proxy, now=clock_now)
        proxy.set_rule(
            FaultRule(
                scenario=FaultScenario.FALSE_SUCCESS_RESPONSE,
                target_id=t_false,
                max_triggers=1,
            )
        )
        rec_f = await gateway.execute_with_permit(
            permit=perm_f, approval=app_f, action=act_f, adapter=proxy, now=clock_now
        )
        assert rec_f.state == EffectState.NEEDS_REVIEW
        assert metrics.snapshot()["counters"]["verifier_false_success_total"] >= 1

        # Verify adapter was automatically quarantined in PostgreSQL
        conf_rec = await gate.get_conformance_record(adapter_id)
        assert conf_rec is not None
        assert conf_rec.status == ConformanceStatus.QUARANTINED
        with pytest.raises(AdapterQuarantinedError):
            await gate.verify_mutation_allowed(proxy, now=clock_now)
    finally:
        async with session_factory() as session:
            await session.execute(
                delete(AdapterConformance).where(AdapterConformance.adapter_id == adapter_id)
            )
            await session.commit()
