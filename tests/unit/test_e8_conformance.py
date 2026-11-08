"""Unit and chaos tests for Feature E8 — Adapter Conformance and Chaos Lab."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.adapters.conformance import (
    AdapterDigestMismatchError,
    AdapterEnablementExpiredError,
    AdapterNotConformantError,
    AdapterQuarantinedError,
    ConformanceRunner,
    ConformanceStatus,
    DefectiveStaleFenceSimulatorAdapter,
    DefectiveVersionCasSimulatorAdapter,
    MutationCapabilityGate,
)
from backend.adapters.fault_proxy import (
    AdapterLookupError,
    AdapterTransportTimeoutError,
    AdapterUnavailableError,
    FaultInjectionProxy,
    FaultRule,
    FaultScenario,
)
from backend.adapters.protocol import (
    AdapterApplyRequest,
    AdapterApplyStatus,
    AdapterLookupStatus,
    compute_adapter_code_digest,
)
from backend.adapters.simulator import DisposableTargetSimulator
from backend.observability.metrics import metrics
from backend.persistence.models import AdapterConformance


@pytest.fixture
async def cleanup_adapter_conformance(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[list[str]]:
    created_ids: list[str] = []
    yield created_ids
    if created_ids:
        async with session_factory() as session:
            await session.execute(
                delete(AdapterConformance).where(AdapterConformance.adapter_id.in_(created_ids))
            )
            await session.commit()


async def test_e8_simulator_protocol_idempotency_fencing_and_version_cas() -> None:
    """Verify DisposableTargetSimulator enforces monotonic fence, expected_version CAS,
    idempotent effect_id replay within retention horizon, and independent read_resource.
    """
    clock_now = datetime(2026, 11, 7, 16, 0, 0, tzinfo=UTC)
    sim = DisposableTargetSimulator(
        adapter_id=f"sim-unit-{uuid.uuid4().hex[:8]}",
        version="1.0.0",
        retention_horizon_seconds=120,
        now_fn=lambda: clock_now,
    )
    tenant_id = uuid.uuid4()
    target_id = "sim-db-primary"

    # Initial read_resource is v1, highest_fence_seen=0
    snap0 = await sim.read_resource(tenant_id, target_id)
    assert snap0.current_version == "v1"
    assert snap0.highest_fence_seen == 0
    assert snap0.healthy is True

    # Apply 1: fence=1, expected_version=v1
    req1 = AdapterApplyRequest(
        tenant_id=tenant_id,
        target_id=target_id,
        effect_id="eff-001",
        fence=1,
        expected_version="v1",
        action_kind="restart_service",
        action_digest="digest-001",
        parameters={"graceful": True},
    )
    res1 = await sim.apply(req1)
    assert res1.status == AdapterApplyStatus.APPLIED
    assert res1.observed_version == "v2"
    assert res1.external_ref is not None

    # Idempotent replay of eff-001 returns DUPLICATE_APPLIED without advancing to v3
    res1_dup = await sim.apply(req1)
    assert res1_dup.status == AdapterApplyStatus.DUPLICATE_APPLIED
    assert res1_dup.observed_version == "v2"
    assert res1_dup.external_ref == res1.external_ref
    assert metrics.snapshot()["counters"]["adapter_duplicate_effects_total"] >= 1

    snap1 = await sim.read_resource(tenant_id, target_id)
    assert snap1.current_version == "v2"
    assert snap1.highest_fence_seen == 1
    assert snap1.last_effect_id == "eff-001"

    # Non-idempotent distinct effect_id trying to reuse fence=1 is rejected as stale fence
    req_reuse_fence = AdapterApplyRequest(
        tenant_id=tenant_id,
        target_id=target_id,
        effect_id="eff-002-reuse-fence",
        fence=1,
        expected_version="v2",
        action_kind="restart_service",
        action_digest="digest-002",
        parameters={},
    )
    res_reuse = await sim.apply(req_reuse_fence)
    assert res_reuse.status == AdapterApplyStatus.REJECTED_STALE_FENCE

    # Version conflict with fence=2, expected_version=v1 (when current is v2)
    req_ver_conflict = AdapterApplyRequest(
        tenant_id=tenant_id,
        target_id=target_id,
        effect_id="eff-002",
        fence=2,
        expected_version="v1",
        action_kind="restart_service",
        action_digest="digest-002",
        parameters={},
    )
    res_vc = await sim.apply(req_ver_conflict)
    assert res_vc.status == AdapterApplyStatus.REJECTED_VERSION_CONFLICT
    assert res_vc.observed_version == "v2"

    # Because fence=2 was presented, highest_fence_seen is now 2; fence=1 is now strictly stale
    res_stale = await sim.apply(req1)
    assert res_stale.status == AdapterApplyStatus.REJECTED_STALE_FENCE


async def test_e8_all_12_fault_injection_scenarios() -> None:
    """Verify FaultInjectionProxy deterministically exercises all 12 required fault scenarios."""
    current_time = datetime(2026, 11, 7, 16, 0, 0, tzinfo=UTC)

    def get_now() -> datetime:
        return current_time

    sim = DisposableTargetSimulator(
        adapter_id=f"sim-chaos-{uuid.uuid4().hex[:8]}",
        version="1.0.0",
        retention_horizon_seconds=60,
        now_fn=get_now,
    )
    proxy = FaultInjectionProxy(sim)
    tenant_id = uuid.uuid4()

    # Scenario 1: DUPLICATE_APPLY
    proxy.set_rule(FaultRule(scenario=FaultScenario.DUPLICATE_APPLY, target_id="t-1"))
    r1 = await proxy.apply(
        AdapterApplyRequest(
            tenant_id=tenant_id,
            target_id="t-1",
            effect_id="e-1",
            fence=1,
            expected_version="v1",
            action_kind="restart_service",
            action_digest="d-1",
        )
    )
    assert r1.status in (AdapterApplyStatus.APPLIED, AdapterApplyStatus.DUPLICATE_APPLIED)
    snap_t1 = await proxy.read_resource(tenant_id, "t-1")
    assert snap_t1.current_version == "v2"

    # Scenario 2: LOST_RESPONSE_AFTER_COMMIT
    proxy.clear_rules()
    proxy.set_rule(FaultRule(scenario=FaultScenario.LOST_RESPONSE_AFTER_COMMIT, target_id="t-2"))
    with pytest.raises(AdapterTransportTimeoutError):
        await proxy.apply(
            AdapterApplyRequest(
                tenant_id=tenant_id,
                target_id="t-2",
                effect_id="e-2",
                fence=1,
                expected_version="v1",
                action_kind="restart_service",
                action_digest="d-2",
            )
        )
    lookup_e2 = await proxy.query_effect(tenant_id, "t-2", "e-2")
    assert lookup_e2.status == AdapterLookupStatus.COMMITTED
    assert lookup_e2.observed_version == "v2"

    # Scenario 3: STALE_FENCE
    proxy.clear_rules()
    proxy.set_rule(FaultRule(scenario=FaultScenario.STALE_FENCE, target_id="t-2"))
    r3 = await proxy.apply(
        AdapterApplyRequest(
            tenant_id=tenant_id,
            target_id="t-2",
            effect_id="e-3",
            fence=2,
            expected_version="v2",
            action_kind="restart_service",
            action_digest="d-3",
        )
    )
    assert r3.status == AdapterApplyStatus.REJECTED_STALE_FENCE

    # Scenario 4: COMPETING_FENCES (n+1 commits first, n is rejected)
    proxy.clear_rules()
    r4_winner = await proxy.apply(
        AdapterApplyRequest(
            tenant_id=tenant_id,
            target_id="t-4",
            effect_id="e-4-high",
            fence=2,
            expected_version="v1",
            action_kind="restart_service",
            action_digest="d-4-high",
        )
    )
    assert r4_winner.status == AdapterApplyStatus.APPLIED
    r4_loser = await proxy.apply(
        AdapterApplyRequest(
            tenant_id=tenant_id,
            target_id="t-4",
            effect_id="e-4-low",
            fence=1,
            expected_version="v1",
            action_kind="restart_service",
            action_digest="d-4-low",
        )
    )
    assert r4_loser.status == AdapterApplyStatus.REJECTED_STALE_FENCE

    # Scenario 5: EXTERNAL_VERSION_CHANGE
    proxy.clear_rules()
    proxy.set_rule(FaultRule(scenario=FaultScenario.EXTERNAL_VERSION_CHANGE, target_id="t-5"))
    r5 = await proxy.apply(
        AdapterApplyRequest(
            tenant_id=tenant_id,
            target_id="t-5",
            effect_id="e-5",
            fence=1,
            expected_version="v1",
            action_kind="restart_service",
            action_digest="d-5",
        )
    )
    assert r5.status == AdapterApplyStatus.REJECTED_VERSION_CONFLICT

    # Scenario 6: PARTIAL_EFFECT
    proxy.clear_rules()
    proxy.set_rule(FaultRule(scenario=FaultScenario.PARTIAL_EFFECT, target_id="t-6"))
    r6 = await proxy.apply(
        AdapterApplyRequest(
            tenant_id=tenant_id,
            target_id="t-6",
            effect_id="e-6",
            fence=1,
            expected_version="v1",
            action_kind="restart_service",
            action_digest="d-6",
        )
    )
    assert r6.status == AdapterApplyStatus.PARTIAL_FAILURE
    snap_t6 = await proxy.read_resource(tenant_id, "t-6")
    assert snap_t6.healthy is False
    lookup_e6 = await proxy.query_effect(tenant_id, "t-6", "e-6")
    assert lookup_e6.status == AdapterLookupStatus.PARTIAL

    # Scenario 7: FALSE_SUCCESS_RESPONSE
    proxy.clear_rules()
    proxy.set_rule(FaultRule(scenario=FaultScenario.FALSE_SUCCESS_RESPONSE, target_id="t-7"))
    r7 = await proxy.apply(
        AdapterApplyRequest(
            tenant_id=tenant_id,
            target_id="t-7",
            effect_id="e-7",
            fence=1,
            expected_version="v1",
            action_kind="restart_service",
            action_digest="d-7",
        )
    )
    assert r7.status == AdapterApplyStatus.APPLIED
    assert r7.observed_version == "v2"
    # Independent read_resource proves target did NOT advance
    snap_t7 = await proxy.read_resource(tenant_id, "t-7")
    assert snap_t7.current_version == "v1"

    # Scenario 8: STATUS_LOOKUP_FAILURE (transient error for max_triggers=1, then succeeds)
    proxy.clear_rules()
    proxy.set_rule(
        FaultRule(
            scenario=FaultScenario.STATUS_LOOKUP_FAILURE,
            target_id="t-2",
            max_triggers=1,
        )
    )
    with pytest.raises(AdapterLookupError):
        await proxy.query_effect(tenant_id, "t-2", "e-2")
    lookup_recovered = await proxy.query_effect(tenant_id, "t-2", "e-2")
    assert lookup_recovered.status == AdapterLookupStatus.COMMITTED

    # Scenario 9: STATUS_STORE_OUTAGE (persistent unavailable error while read_resource works)
    proxy.clear_rules()
    proxy.set_rule(FaultRule(scenario=FaultScenario.STATUS_STORE_OUTAGE, target_id="t-2"))
    with pytest.raises(AdapterUnavailableError):
        await proxy.query_effect(tenant_id, "t-2", "e-2")
    snap_t2 = await proxy.read_resource(tenant_id, "t-2")
    assert snap_t2.current_version == "v2"

    # Scenario 10: RETENTION_EXPIRY
    proxy.clear_rules()
    current_time = current_time + timedelta(seconds=120)
    lookup_expired = await proxy.query_effect(tenant_id, "t-2", "e-2")
    assert lookup_expired.status == AdapterLookupStatus.RETENTION_EXPIRED
    assert metrics.snapshot()["counters"]["adapter_retention_gaps_total"] >= 1

    # Scenario 11: ADAPTER_VERSION_CHANGE
    orig_digest = proxy.profile().code_digest
    proxy.set_rule(
        FaultRule(
            scenario=FaultScenario.ADAPTER_VERSION_CHANGE,
            override_version="2.0.0-unverified",
        )
    )
    assert proxy.profile().version == "2.0.0-unverified"
    assert proxy.profile().code_digest != orig_digest


async def test_e8_conformance_runner_gate_quarantine_digest_and_expiry(
    session_factory: async_sessionmaker[AsyncSession],
    cleanup_adapter_conformance: list[str],
) -> None:
    """Verify ConformanceRunner executes all 12 scenarios, certifies conformant adapters,
    quarantines defective adapters (Scenario 12), and enforces digest and expiry checks.
    """
    adapter_id = f"sim-cert-{uuid.uuid4().hex[:8]}"
    cleanup_adapter_conformance.append(adapter_id)

    sim = DisposableTargetSimulator(adapter_id=adapter_id, version="1.0.0")
    gate = MutationCapabilityGate(session_factory)
    runner = ConformanceRunner(session_factory)

    # 1. Before running conformance, adapter is unverified and blocked from mutation
    with pytest.raises(AdapterNotConformantError):
        await gate.verify_mutation_allowed(sim)

    # 2. Run full 12-scenario conformance and chaos suite
    report = await runner.run_conformance_suite(sim, validity_seconds=3600)
    assert report.passed is True
    assert report.status == ConformanceStatus.CONFORMANT
    assert len(report.scenario_results) == 12
    assert all(sr.passed for sr in report.scenario_results)

    # 3. Gate now permits mutation for exact (adapter_id, version, code_digest)
    record = await gate.verify_mutation_allowed(sim)
    assert record.status == ConformanceStatus.CONFORMANT
    assert record.code_digest == compute_adapter_code_digest(sim)

    # 4. Scenario 11: Adapter version / code digest change immediately revokes enablement
    tampered_proxy = FaultInjectionProxy(sim)
    tampered_proxy.set_rule(
        FaultRule(
            scenario=FaultScenario.ADAPTER_VERSION_CHANGE,
            override_version="1.0.1-hotfix",
        )
    )
    with pytest.raises(AdapterDigestMismatchError):
        await gate.verify_mutation_allowed(tampered_proxy)

    # Verify revocation was durably committed to PostgreSQL
    persisted = await gate.get_conformance_record(adapter_id)
    assert persisted is not None
    assert persisted.status == ConformanceStatus.REVOKED_DIGEST_MISMATCH

    # 5. Re-certify with short validity and verify expiration blocks mutation
    await runner.run_conformance_suite(sim, validity_seconds=10)
    future_now = datetime.now(UTC) + timedelta(seconds=30)
    with pytest.raises(AdapterEnablementExpiredError):
        await gate.verify_mutation_allowed(sim, now=future_now)
    persisted_exp = await gate.get_conformance_record(adapter_id)
    assert persisted_exp is not None
    assert persisted_exp.status == ConformanceStatus.EXPIRED

    # 6. Scenario 12: Defective adapters (stale fence / broken CAS) fail conformance and are QUARANTINED
    bad_fence_id = f"sim-bad-fence-{uuid.uuid4().hex[:8]}"
    cleanup_adapter_conformance.append(bad_fence_id)
    bad_fence_adapter = DefectiveStaleFenceSimulatorAdapter(
        adapter_id=bad_fence_id, version="1.0.0"
    )
    bad_report = await runner.run_conformance_suite(bad_fence_adapter)
    assert bad_report.passed is False
    assert bad_report.status == ConformanceStatus.QUARANTINED
    with pytest.raises(AdapterQuarantinedError):
        await gate.verify_mutation_allowed(bad_fence_adapter)

    bad_cas_id = f"sim-bad-cas-{uuid.uuid4().hex[:8]}"
    cleanup_adapter_conformance.append(bad_cas_id)
    bad_cas_adapter = DefectiveVersionCasSimulatorAdapter(adapter_id=bad_cas_id, version="1.0.0")
    bad_cas_report = await runner.run_conformance_suite(bad_cas_adapter)
    assert bad_cas_report.passed is False
    assert bad_cas_report.status == ConformanceStatus.QUARANTINED
