"""E8 Adapter Conformance Runner, Durable Registry, and Mutation Capability Gate."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
    AdapterApplyResponse,
    AdapterApplyStatus,
    AdapterCapabilityProfile,
    AdapterLookupStatus,
    TargetAdapterProtocol,
    compute_adapter_code_digest,
)
from backend.adapters.simulator import DisposableTargetSimulator
from backend.domain.incident import DomainError
from backend.observability.metrics import emit_event, metrics
from backend.persistence.models import AdapterConformance


class ConformanceStatus(StrEnum):
    READ_ONLY_UNVERIFIED = "READ_ONLY_UNVERIFIED"
    CONFORMANT = "CONFORMANT"
    QUARANTINED = "QUARANTINED"
    REVOKED_DIGEST_MISMATCH = "REVOKED_DIGEST_MISMATCH"
    EXPIRED = "EXPIRED"


class AdapterConformanceError(DomainError):
    """Base error for E8 adapter conformance and capability gate rejections."""


class AdapterNotConformantError(AdapterConformanceError):
    def __init__(self, adapter_id: str, reason: str = "") -> None:
        self.adapter_id = adapter_id
        super().__init__(f"Adapter '{adapter_id}' is not certified CONFORMANT: {reason}")


class AdapterQuarantinedError(AdapterConformanceError):
    def __init__(self, adapter_id: str, reason: str = "") -> None:
        self.adapter_id = adapter_id
        super().__init__(f"Adapter '{adapter_id}' is QUARANTINED: {reason}")


class AdapterDigestMismatchError(AdapterConformanceError):
    def __init__(self, adapter_id: str, expected_digest: str, actual_digest: str) -> None:
        self.adapter_id = adapter_id
        self.expected_digest = expected_digest
        self.actual_digest = actual_digest
        super().__init__(
            f"Adapter '{adapter_id}' code/version digest mismatch: "
            f"certified={expected_digest[:12]} != runtime={actual_digest[:12]}"
        )


class AdapterEnablementExpiredError(AdapterConformanceError):
    def __init__(self, adapter_id: str, expires_at: datetime) -> None:
        self.adapter_id = adapter_id
        self.expires_at = expires_at
        super().__init__(f"Adapter '{adapter_id}' conformance expired at {expires_at.isoformat()}")


@dataclass(frozen=True)
class ScenarioCheckResult:
    scenario: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class ConformanceReport:
    adapter_id: str
    adapter_version: str
    code_digest: str
    status: ConformanceStatus
    passed: bool
    verified_at: datetime
    expires_at: datetime | None
    scenario_results: tuple[ScenarioCheckResult, ...]


@dataclass(frozen=True)
class ConformanceRecordSnapshot:
    adapter_id: str
    adapter_version: str
    code_digest: str
    status: ConformanceStatus
    retention_horizon_seconds: int
    capabilities: dict[str, Any]
    evidence: dict[str, Any]
    verified_at: datetime | None
    expires_at: datetime | None
    updated_at: datetime


class DefectiveStaleFenceSimulatorAdapter(DisposableTargetSimulator):
    """Negative-control defective adapter (Scenario 12) that fails to reject stale fences."""

    async def apply(self, request: AdapterApplyRequest) -> AdapterApplyResponse:
        # Bug: overwrites highest_fence_seen downward so stale fences are accepted
        with self._lock:
            res = self._get_or_create_resource(request.tenant_id, request.target_id)
            if request.fence > 0 and request.fence < res.highest_fence_seen:
                res.highest_fence_seen = request.fence - 1
        return await super().apply(request)


class DefectiveVersionCasSimulatorAdapter(DisposableTargetSimulator):
    """Negative-control defective adapter (Scenario 12) that ignores expected_version CAS."""

    async def apply(self, request: AdapterApplyRequest) -> AdapterApplyResponse:
        with self._lock:
            res = self._get_or_create_resource(request.tenant_id, request.target_id)
            patched_req = AdapterApplyRequest(
                tenant_id=request.tenant_id,
                target_id=request.target_id,
                effect_id=request.effect_id,
                fence=request.fence,
                expected_version=res.current_version,
                action_kind=request.action_kind,
                action_digest=request.action_digest,
                parameters=request.parameters,
                correlation_id=request.correlation_id,
            )
        return await super().apply(patched_req)


class MutationCapabilityGate:
    """Fail-closed capability gate verifying E8 adapter conformance, digest, and expiry."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_conformance_record(self, adapter_id: str) -> ConformanceRecordSnapshot | None:
        async with self._session_factory() as session:
            row = await session.get(AdapterConformance, adapter_id)
            if row is None:
                return None
            return self._to_snapshot(row)

    async def quarantine_adapter(
        self,
        adapter: TargetAdapterProtocol,
        *,
        reason: str,
        correlation_id: str = "",
        now: datetime | None = None,
    ) -> ConformanceRecordSnapshot:
        """Atomically quarantine an adapter in a dedicated transaction that survives outer rollbacks."""
        ts = now or datetime.now(UTC)
        prof = adapter.profile()
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(AdapterConformance)
                    .where(AdapterConformance.adapter_id == prof.adapter_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()

            evidence = {"quarantine_reason": reason, "quarantined_at": ts.isoformat()}
            if row is None:
                row = AdapterConformance(
                    adapter_id=prof.adapter_id,
                    adapter_version=prof.version,
                    code_digest=prof.code_digest,
                    status=ConformanceStatus.QUARANTINED.value,
                    retention_horizon_seconds=prof.retention_horizon_seconds,
                    capabilities_json=prof.to_dict(),
                    conformance_evidence_json=evidence,
                    verified_at=None,
                    expires_at=None,
                    updated_at=ts,
                )
                session.add(row)
            else:
                row.status = ConformanceStatus.QUARANTINED.value
                merged = dict(row.conformance_evidence_json or {})
                merged.update(evidence)
                row.conformance_evidence_json = merged
                row.updated_at = ts

            await session.commit()
            snap = self._to_snapshot(row)

        metrics.inc("adapter_enablement_revoked_total")
        emit_event(
            "adapter.enablement_revoked",
            correlation_id=correlation_id,
            adapter_id=prof.adapter_id,
            adapter_version=prof.version,
            code_digest=prof.code_digest,
            new_status=ConformanceStatus.QUARANTINED.value,
            reason=reason,
            level="ERROR",
        )
        return snap

    async def verify_mutation_allowed(
        self,
        adapter: TargetAdapterProtocol,
        *,
        correlation_id: str = "",
        now: datetime | None = None,
    ) -> ConformanceRecordSnapshot:
        """Verify that `adapter` is certified CONFORMANT, matches version & code_digest,
        has not expired, and declares all required safety capabilities.

        If digest/version drifted or conformance expired, commits the revocation in a
        dedicated transaction before raising.
        """
        check_time = now or datetime.now(UTC)
        prof: AdapterCapabilityProfile = adapter.profile()
        runtime_digest = compute_adapter_code_digest(adapter)

        # 1. Verify mandatory capability flags
        if not (
            prof.supports_idempotency
            and prof.supports_fencing
            and prof.supports_version_cas
            and prof.supports_status_lookup
            and prof.retention_horizon_seconds >= 60
        ):
            raise AdapterNotConformantError(
                prof.adapter_id,
                "Adapter capability profile lacks mandatory fencing/CAS/idempotency/lookup support",
            )

        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(AdapterConformance)
                    .where(AdapterConformance.adapter_id == prof.adapter_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()

            if row is None:
                raise AdapterNotConformantError(
                    prof.adapter_id, "No conformance record registered in PostgreSQL"
                )

            current_status = ConformanceStatus(row.status)
            if current_status == ConformanceStatus.QUARANTINED:
                raise AdapterQuarantinedError(
                    prof.adapter_id,
                    str(
                        (row.conformance_evidence_json or {}).get(
                            "quarantine_reason", "quarantined"
                        )
                    ),
                )

            # 2. Check version or code_digest drift BEFORE trusting status
            if row.adapter_version != prof.version or row.code_digest != runtime_digest:
                expected_digest = row.code_digest
                row.status = ConformanceStatus.REVOKED_DIGEST_MISMATCH.value
                row.updated_at = check_time
                await session.commit()
                metrics.inc("adapter_enablement_revoked_total")
                emit_event(
                    "adapter.enablement_revoked",
                    correlation_id=correlation_id,
                    adapter_id=prof.adapter_id,
                    certified_version=row.adapter_version,
                    runtime_version=prof.version,
                    certified_digest=expected_digest,
                    runtime_digest=runtime_digest,
                    new_status=ConformanceStatus.REVOKED_DIGEST_MISMATCH.value,
                    reason="version_or_code_digest_mismatch",
                    level="ERROR",
                )
                raise AdapterDigestMismatchError(prof.adapter_id, expected_digest, runtime_digest)

            # 3. Check conformance expiry
            if row.expires_at is None or row.expires_at <= check_time:
                exp_ts = row.expires_at or check_time
                row.status = ConformanceStatus.EXPIRED.value
                row.updated_at = check_time
                await session.commit()
                metrics.inc("adapter_enablement_revoked_total")
                emit_event(
                    "adapter.enablement_revoked",
                    correlation_id=correlation_id,
                    adapter_id=prof.adapter_id,
                    new_status=ConformanceStatus.EXPIRED.value,
                    expires_at=exp_ts.isoformat(),
                    reason="conformance_expired",
                    level="WARNING",
                )
                raise AdapterEnablementExpiredError(prof.adapter_id, exp_ts)

            if current_status != ConformanceStatus.CONFORMANT:
                raise AdapterNotConformantError(
                    prof.adapter_id, f"Current status is {current_status.value}"
                )

            return self._to_snapshot(row)

    @staticmethod
    def _to_snapshot(row: AdapterConformance) -> ConformanceRecordSnapshot:
        return ConformanceRecordSnapshot(
            adapter_id=row.adapter_id,
            adapter_version=row.adapter_version,
            code_digest=row.code_digest,
            status=ConformanceStatus(row.status),
            retention_horizon_seconds=row.retention_horizon_seconds,
            capabilities=dict(row.capabilities_json or {}),
            evidence=dict(row.conformance_evidence_json or {}),
            verified_at=row.verified_at,
            expires_at=row.expires_at,
            updated_at=row.updated_at,
        )


class ConformanceRunner:
    """Executes the 12-scenario E8 conformance and chaos campaign against an adapter."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def run_conformance_suite(
        self,
        adapter: TargetAdapterProtocol,
        *,
        validity_seconds: int = 86400,
        now: datetime | None = None,
    ) -> ConformanceReport:
        """Run all 12 E8 conformance & fault-injection scenarios against an isolated test harness
        of `adapter`'s class/configuration, and persist the certification or quarantine in PostgreSQL.
        """
        run_time = now or datetime.now(UTC)
        prof = adapter.profile()
        code_digest = compute_adapter_code_digest(adapter)

        results: list[ScenarioCheckResult] = []
        clock_cursor = run_time

        def get_clock() -> datetime:
            return clock_cursor

        # Instantiate a fresh disposable test instance of the same adapter class for isolated chaos testing
        adapter_cls = adapter.__class__
        if issubclass(adapter_cls, DisposableTargetSimulator):
            test_sim: TargetAdapterProtocol = adapter_cls(
                adapter_id=prof.adapter_id,
                version=prof.version,
                retention_horizon_seconds=prof.retention_horizon_seconds,
                now_fn=get_clock,
            )
        else:
            test_sim = adapter

        proxy = FaultInjectionProxy(test_sim)
        tenant_id = uuid.uuid4()

        # Check capability profile flags first
        profile_ok = (
            prof.supports_idempotency
            and prof.supports_fencing
            and prof.supports_version_cas
            and prof.supports_status_lookup
            and prof.retention_horizon_seconds >= 60
        )

        # Scenario 1: DUPLICATE_APPLY
        proxy.clear_rules()
        proxy.set_rule(FaultRule(scenario=FaultScenario.DUPLICATE_APPLY, target_id="conf-t1"))
        r1 = await proxy.apply(
            AdapterApplyRequest(
                tenant_id=tenant_id,
                target_id="conf-t1",
                effect_id="conf-e1",
                fence=1,
                expected_version="v1",
                action_kind="restart_service",
                action_digest="dig-1",
            )
        )
        s1 = await proxy.read_resource(tenant_id, "conf-t1")
        s1_pass = (
            r1.status == AdapterApplyStatus.DUPLICATE_APPLIED
            and s1.current_version == "v2"
            and s1.state_data.get("mutation_count") == 1
        )
        results.append(
            ScenarioCheckResult(
                scenario=FaultScenario.DUPLICATE_APPLY.value,
                passed=s1_pass,
                detail=f"status={r1.status}, version={s1.current_version}",
            )
        )

        # Scenario 2: LOST_RESPONSE_AFTER_COMMIT
        proxy.clear_rules()
        proxy.set_rule(
            FaultRule(scenario=FaultScenario.LOST_RESPONSE_AFTER_COMMIT, target_id="conf-t2")
        )
        timed_out = False
        try:
            await proxy.apply(
                AdapterApplyRequest(
                    tenant_id=tenant_id,
                    target_id="conf-t2",
                    effect_id="conf-e2",
                    fence=1,
                    expected_version="v1",
                    action_kind="restart_service",
                    action_digest="dig-2",
                )
            )
        except AdapterTransportTimeoutError:
            timed_out = True
        lk2 = await proxy.query_effect(tenant_id, "conf-t2", "conf-e2")
        s2_pass = (
            timed_out
            and lk2.status == AdapterLookupStatus.COMMITTED
            and lk2.observed_version == "v2"
        )
        results.append(
            ScenarioCheckResult(
                scenario=FaultScenario.LOST_RESPONSE_AFTER_COMMIT.value,
                passed=s2_pass,
                detail=f"timed_out={timed_out}, lookup={lk2.status}",
            )
        )

        # Scenario 3: STALE_FENCE
        proxy.clear_rules()
        # Advance conf-t3 to fence=2 first
        await proxy.apply(
            AdapterApplyRequest(
                tenant_id=tenant_id,
                target_id="conf-t3",
                effect_id="conf-e3-f2",
                fence=2,
                expected_version="v1",
                action_kind="restart_service",
                action_digest="dig-3a",
            )
        )
        r3_stale = await proxy.apply(
            AdapterApplyRequest(
                tenant_id=tenant_id,
                target_id="conf-t3",
                effect_id="conf-e3-f1",
                fence=1,
                expected_version="v2",
                action_kind="restart_service",
                action_digest="dig-3b",
            )
        )
        s3_pass = r3_stale.status == AdapterApplyStatus.REJECTED_STALE_FENCE
        results.append(
            ScenarioCheckResult(
                scenario=FaultScenario.STALE_FENCE.value,
                passed=s3_pass,
                detail=f"stale_fence_status={r3_stale.status}",
            )
        )

        # Scenario 4: COMPETING_FENCES
        proxy.clear_rules()
        r4_high = await proxy.apply(
            AdapterApplyRequest(
                tenant_id=tenant_id,
                target_id="conf-t4",
                effect_id="conf-e4-high",
                fence=5,
                expected_version="v1",
                action_kind="drain_traffic",
                action_digest="dig-4-high",
            )
        )
        r4_low = await proxy.apply(
            AdapterApplyRequest(
                tenant_id=tenant_id,
                target_id="conf-t4",
                effect_id="conf-e4-low",
                fence=4,
                expected_version="v1",
                action_kind="drain_traffic",
                action_digest="dig-4-low",
            )
        )
        s4_pass = (
            r4_high.status == AdapterApplyStatus.APPLIED
            and r4_low.status == AdapterApplyStatus.REJECTED_STALE_FENCE
        )
        results.append(
            ScenarioCheckResult(
                scenario=FaultScenario.COMPETING_FENCES.value,
                passed=s4_pass,
                detail=f"high={r4_high.status}, low={r4_low.status}",
            )
        )

        # Scenario 5: EXTERNAL_VERSION_CHANGE
        proxy.clear_rules()
        proxy.set_rule(
            FaultRule(scenario=FaultScenario.EXTERNAL_VERSION_CHANGE, target_id="conf-t5")
        )
        r5 = await proxy.apply(
            AdapterApplyRequest(
                tenant_id=tenant_id,
                target_id="conf-t5",
                effect_id="conf-e5",
                fence=1,
                expected_version="v1",
                action_kind="restart_service",
                action_digest="dig-5",
            )
        )
        s5_pass = r5.status == AdapterApplyStatus.REJECTED_VERSION_CONFLICT
        results.append(
            ScenarioCheckResult(
                scenario=FaultScenario.EXTERNAL_VERSION_CHANGE.value,
                passed=s5_pass,
                detail=f"version_conflict_status={r5.status}",
            )
        )

        # Scenario 6: PARTIAL_EFFECT
        proxy.clear_rules()
        proxy.set_rule(FaultRule(scenario=FaultScenario.PARTIAL_EFFECT, target_id="conf-t6"))
        r6 = await proxy.apply(
            AdapterApplyRequest(
                tenant_id=tenant_id,
                target_id="conf-t6",
                effect_id="conf-e6",
                fence=1,
                expected_version="v1",
                action_kind="restart_service",
                action_digest="dig-6",
            )
        )
        lk6 = await proxy.query_effect(tenant_id, "conf-t6", "conf-e6")
        snap6 = await proxy.read_resource(tenant_id, "conf-t6")
        s6_pass = (
            r6.status == AdapterApplyStatus.PARTIAL_FAILURE
            and lk6.status == AdapterLookupStatus.PARTIAL
            and snap6.healthy is False
        )
        results.append(
            ScenarioCheckResult(
                scenario=FaultScenario.PARTIAL_EFFECT.value,
                passed=s6_pass,
                detail=f"apply={r6.status}, lookup={lk6.status}, healthy={snap6.healthy}",
            )
        )

        # Scenario 7: FALSE_SUCCESS_RESPONSE
        proxy.clear_rules()
        proxy.set_rule(
            FaultRule(scenario=FaultScenario.FALSE_SUCCESS_RESPONSE, target_id="conf-t7")
        )
        r7 = await proxy.apply(
            AdapterApplyRequest(
                tenant_id=tenant_id,
                target_id="conf-t7",
                effect_id="conf-e7",
                fence=1,
                expected_version="v1",
                action_kind="restart_service",
                action_digest="dig-7",
            )
        )
        snap7 = await proxy.read_resource(tenant_id, "conf-t7")
        s7_pass = (
            r7.status == AdapterApplyStatus.APPLIED
            and r7.observed_version == "v2"
            and snap7.current_version == "v1"
        )
        results.append(
            ScenarioCheckResult(
                scenario=FaultScenario.FALSE_SUCCESS_RESPONSE.value,
                passed=s7_pass,
                detail=f"claimed={r7.observed_version}, actual={snap7.current_version}",
            )
        )

        # Scenario 8: STATUS_LOOKUP_FAILURE
        proxy.clear_rules()
        proxy.set_rule(
            FaultRule(
                scenario=FaultScenario.STATUS_LOOKUP_FAILURE,
                target_id="conf-t2",
                max_triggers=1,
            )
        )
        s8_err = False
        try:
            await proxy.query_effect(tenant_id, "conf-t2", "conf-e2")
        except AdapterLookupError:
            s8_err = True
        lk8 = await proxy.query_effect(tenant_id, "conf-t2", "conf-e2")
        s8_pass = s8_err and lk8.status == AdapterLookupStatus.COMMITTED
        results.append(
            ScenarioCheckResult(
                scenario=FaultScenario.STATUS_LOOKUP_FAILURE.value,
                passed=s8_pass,
                detail=f"transient_err={s8_err}, retry_lookup={lk8.status}",
            )
        )

        # Scenario 9: STATUS_STORE_OUTAGE
        proxy.clear_rules()
        proxy.set_rule(FaultRule(scenario=FaultScenario.STATUS_STORE_OUTAGE, target_id="conf-t2"))
        s9_outage = False
        try:
            await proxy.query_effect(tenant_id, "conf-t2", "conf-e2")
        except AdapterUnavailableError:
            s9_outage = True
        snap9 = await proxy.read_resource(tenant_id, "conf-t2")
        s9_pass = s9_outage and snap9.current_version == "v2"
        results.append(
            ScenarioCheckResult(
                scenario=FaultScenario.STATUS_STORE_OUTAGE.value,
                passed=s9_pass,
                detail=f"outage_raised={s9_outage}, read_resource_version={snap9.current_version}",
            )
        )

        # Scenario 10: RETENTION_EXPIRY
        proxy.clear_rules()
        clock_cursor = run_time + timedelta(seconds=prof.retention_horizon_seconds + 10)
        lk10 = await proxy.query_effect(tenant_id, "conf-t2", "conf-e2")
        clock_cursor = run_time
        s10_pass = lk10.status == AdapterLookupStatus.RETENTION_EXPIRED
        results.append(
            ScenarioCheckResult(
                scenario=FaultScenario.RETENTION_EXPIRY.value,
                passed=s10_pass,
                detail=f"expired_lookup={lk10.status}",
            )
        )

        # Scenario 11: ADAPTER_VERSION_CHANGE
        proxy.clear_rules()
        proxy.set_rule(
            FaultRule(
                scenario=FaultScenario.ADAPTER_VERSION_CHANGE,
                override_version=f"{prof.version}-tampered",
            )
        )
        drifted_prof = proxy.profile()
        s11_pass = drifted_prof.version != prof.version and drifted_prof.code_digest != code_digest
        results.append(
            ScenarioCheckResult(
                scenario=FaultScenario.ADAPTER_VERSION_CHANGE.value,
                passed=s11_pass,
                detail=f"drifted_digest_differs={s11_pass}",
            )
        )

        # Scenario 12: CONFORMANCE_INVALIDATION (all prior 11 checks + profile check must hold)
        all_prior_passed = profile_ok and all(r.passed for r in results)
        results.append(
            ScenarioCheckResult(
                scenario=FaultScenario.CONFORMANCE_INVALIDATION.value,
                passed=all_prior_passed,
                detail=f"profile_ok={profile_ok}, all_scenarios_ok={all_prior_passed}",
            )
        )

        suite_passed = profile_ok and all(r.passed for r in results)
        final_status = (
            ConformanceStatus.CONFORMANT if suite_passed else ConformanceStatus.QUARANTINED
        )
        expires_at = run_time + timedelta(seconds=validity_seconds) if suite_passed else None
        failed_names = [r.scenario for r in results if not r.passed]
        evidence_payload: dict[str, Any] = {
            "passed": suite_passed,
            "verified_at": run_time.isoformat(),
            "scenarios": [
                {"scenario": r.scenario, "passed": r.passed, "detail": r.detail} for r in results
            ],
        }
        if not suite_passed:
            evidence_payload["quarantine_reason"] = (
                f"Failed conformance scenarios: {', '.join(failed_names)}"
            )

        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(AdapterConformance)
                    .where(AdapterConformance.adapter_id == prof.adapter_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()

            if row is None:
                row = AdapterConformance(
                    adapter_id=prof.adapter_id,
                    adapter_version=prof.version,
                    code_digest=code_digest,
                    status=final_status.value,
                    retention_horizon_seconds=prof.retention_horizon_seconds,
                    capabilities_json=prof.to_dict(),
                    conformance_evidence_json=evidence_payload,
                    verified_at=run_time if suite_passed else None,
                    expires_at=expires_at,
                    updated_at=run_time,
                )
                session.add(row)
            else:
                row.adapter_version = prof.version
                row.code_digest = code_digest
                row.status = final_status.value
                row.retention_horizon_seconds = prof.retention_horizon_seconds
                row.capabilities_json = prof.to_dict()
                row.conformance_evidence_json = evidence_payload
                row.verified_at = run_time if suite_passed else None
                row.expires_at = expires_at
                row.updated_at = run_time

            await session.commit()

        if suite_passed:
            metrics.inc("adapter_conformance_pass_total")
            emit_event(
                "adapter.conformance_pass",
                adapter_id=prof.adapter_id,
                adapter_version=prof.version,
                code_digest=code_digest,
                scenarios_passed=len(results),
            )
        else:
            metrics.inc("adapter_conformance_fail_total")
            metrics.inc("adapter_enablement_revoked_total")
            emit_event(
                "adapter.enablement_revoked",
                adapter_id=prof.adapter_id,
                adapter_version=prof.version,
                code_digest=code_digest,
                new_status=ConformanceStatus.QUARANTINED.value,
                failed_scenarios=failed_names,
                level="ERROR",
            )

        return ConformanceReport(
            adapter_id=prof.adapter_id,
            adapter_version=prof.version,
            code_digest=code_digest,
            status=final_status,
            passed=suite_passed,
            verified_at=run_time,
            expires_at=expires_at,
            scenario_results=tuple(results),
        )
