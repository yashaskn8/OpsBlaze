"""Disposable, tenant-scoped target simulator for Feature E8, B2, and B3."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from backend.adapters.protocol import (
    SUPPORTED_SIMULATOR_ACTIONS,
    AdapterApplyRequest,
    AdapterApplyResponse,
    AdapterApplyStatus,
    AdapterCapabilityProfile,
    AdapterEffectLookupResult,
    AdapterLookupStatus,
    ResourceStateSnapshot,
    compute_adapter_code_digest,
)
from backend.observability.metrics import emit_event, metrics

ALL_FAULT_SCENARIO_NAMES: tuple[str, ...] = (
    "DUPLICATE_APPLY",
    "LOST_RESPONSE_AFTER_COMMIT",
    "STALE_FENCE",
    "COMPETING_FENCES",
    "EXTERNAL_VERSION_CHANGE",
    "PARTIAL_EFFECT",
    "FALSE_SUCCESS_RESPONSE",
    "STATUS_LOOKUP_FAILURE",
    "STATUS_STORE_OUTAGE",
    "RETENTION_EXPIRY",
    "ADAPTER_VERSION_CHANGE",
    "CONFORMANCE_INVALIDATION",
)


@dataclass
class RecordedSimulatorEffect:
    effect_id: str
    fence: int
    expected_version: str
    observed_version: str
    action_kind: str
    action_digest: str
    external_ref: str
    committed_at: datetime
    status: AdapterLookupStatus = AdapterLookupStatus.COMMITTED


@dataclass
class SimulatedTargetResource:
    tenant_id: uuid.UUID
    target_id: str
    current_version: str = "v1"
    version_counter: int = 1
    highest_fence_seen: int = 0
    last_effect_id: str | None = None
    healthy: bool = True
    state_data: dict[str, Any] = field(
        default_factory=lambda: {
            "service_state": "running",
            "traffic_drained": False,
            "worker_scale": 2,
            "config_revision": "rev-1",
            "mutation_count": 0,
        }
    )
    effect_log: dict[str, RecordedSimulatorEffect] = field(default_factory=dict)


class DisposableTargetSimulator:
    """Isolated, tenant-scoped in-memory target simulator with strict fencing, CAS, and idempotency."""

    def __init__(
        self,
        *,
        adapter_id: str = "sim-remediation-v1",
        version: str = "1.0.0",
        retention_horizon_seconds: int = 300,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._adapter_id = adapter_id
        self._version = version
        self._retention_horizon_seconds = retention_horizon_seconds
        self._now_fn = now_fn or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()
        self._resources: dict[tuple[uuid.UUID, str], SimulatedTargetResource] = {}
        self._code_digest = compute_adapter_code_digest(
            self.__class__,
            adapter_id=adapter_id,
            version=version,
            retention_horizon_seconds=retention_horizon_seconds,
        )

    def profile(self) -> AdapterCapabilityProfile:
        return AdapterCapabilityProfile(
            adapter_id=self._adapter_id,
            version=self._version,
            code_digest=self._code_digest,
            supports_idempotency=True,
            supports_fencing=True,
            supports_version_cas=True,
            supports_status_lookup=True,
            retention_horizon_seconds=self._retention_horizon_seconds,
            is_simulator=True,
            supported_actions=SUPPORTED_SIMULATOR_ACTIONS,
            supported_fault_scenarios=ALL_FAULT_SCENARIO_NAMES,
        )

    def _get_or_create_resource(
        self, tenant_id: uuid.UUID, target_id: str
    ) -> SimulatedTargetResource:
        key = (tenant_id, target_id)
        res = self._resources.get(key)
        if res is None:
            res = SimulatedTargetResource(tenant_id=tenant_id, target_id=target_id)
            self._resources[key] = res
        return res

    def advance_version_out_of_band(self, tenant_id: uuid.UUID, target_id: str) -> str:
        """Simulate an external out-of-band resource version change (Scenario 5)."""
        with self._lock:
            res = self._get_or_create_resource(tenant_id, target_id)
            res.version_counter += 1
            res.current_version = f"v{res.version_counter}"
            res.state_data["out_of_band_change"] = True
            return res.current_version

    def record_partial_effect(self, request: AdapterApplyRequest) -> AdapterApplyResponse:
        """Simulate a dirty partial effect (Scenario 6) that leaves target unhealthy."""
        now = self._now_fn()
        with self._lock:
            res = self._get_or_create_resource(request.tenant_id, request.target_id)
            res.highest_fence_seen = max(res.highest_fence_seen, request.fence)
            res.healthy = False
            res.state_data["partial_corruption"] = True
            ext_ref = f"sim-partial-{request.effect_id[:12]}"
            res.effect_log[request.effect_id] = RecordedSimulatorEffect(
                effect_id=request.effect_id,
                fence=request.fence,
                expected_version=request.expected_version,
                observed_version=res.current_version,
                action_kind=request.action_kind,
                action_digest=request.action_digest,
                external_ref=ext_ref,
                committed_at=now,
                status=AdapterLookupStatus.PARTIAL,
            )
            return AdapterApplyResponse(
                status=AdapterApplyStatus.PARTIAL_FAILURE,
                external_ref=ext_ref,
                observed_version=res.current_version,
                detail="Simulated partial effect left target in dirty state",
            )

    async def apply(self, request: AdapterApplyRequest) -> AdapterApplyResponse:
        now = self._now_fn()
        with self._lock:
            res = self._get_or_create_resource(request.tenant_id, request.target_id)

            # 1. Positive fence guard
            if request.fence <= 0:
                metrics.inc("adapter_stale_fence_violations_total")
                emit_event(
                    "adapter.stale_fence_violation",
                    correlation_id=request.correlation_id,
                    adapter_id=self._adapter_id,
                    target_id=request.target_id,
                    fence=request.fence,
                    highest_fence_seen=res.highest_fence_seen,
                    reason="non_positive_fence",
                    level="WARNING",
                )
                return AdapterApplyResponse(
                    status=AdapterApplyStatus.REJECTED_STALE_FENCE,
                    observed_version=res.current_version,
                    detail=f"Fence must be positive, got {request.fence}",
                )

            # 2. Stale fence guard (strictly superseded by a higher fence)
            if request.fence < res.highest_fence_seen:
                metrics.inc("adapter_stale_fence_violations_total")
                emit_event(
                    "adapter.stale_fence_violation",
                    correlation_id=request.correlation_id,
                    adapter_id=self._adapter_id,
                    target_id=request.target_id,
                    fence=request.fence,
                    highest_fence_seen=res.highest_fence_seen,
                    reason="fence_superseded",
                    level="WARNING",
                )
                return AdapterApplyResponse(
                    status=AdapterApplyStatus.REJECTED_STALE_FENCE,
                    observed_version=res.current_version,
                    detail=(
                        f"Stale fence {request.fence} < highest_fence_seen {res.highest_fence_seen}"
                    ),
                )

            # 3. Idempotency check within retention horizon
            existing = res.effect_log.get(request.effect_id)
            if existing is not None:
                age_sec = (now - existing.committed_at).total_seconds()
                if age_sec < self._retention_horizon_seconds:
                    if (
                        existing.fence != request.fence
                        or existing.action_digest != request.action_digest
                        or existing.expected_version != request.expected_version
                    ):
                        return AdapterApplyResponse(
                            status=AdapterApplyStatus.FAILED,
                            observed_version=res.current_version,
                            detail="Conflicting payload for existing effect_id",
                        )
                    if existing.status == AdapterLookupStatus.PARTIAL:
                        return AdapterApplyResponse(
                            status=AdapterApplyStatus.PARTIAL_FAILURE,
                            external_ref=existing.external_ref,
                            observed_version=existing.observed_version,
                            detail="Previously recorded partial effect",
                        )
                    metrics.inc("adapter_duplicate_effects_total")
                    emit_event(
                        "adapter.duplicate_effect",
                        correlation_id=request.correlation_id,
                        adapter_id=self._adapter_id,
                        target_id=request.target_id,
                        effect_id=request.effect_id,
                        fence=request.fence,
                    )
                    return AdapterApplyResponse(
                        status=AdapterApplyStatus.DUPLICATE_APPLIED,
                        external_ref=existing.external_ref,
                        observed_version=existing.observed_version,
                        detail="Idempotent duplicate apply within retention horizon",
                    )

            # 4. Non-idempotent same-fence guard: a different effect_id cannot reuse an already-used fence
            if (
                request.fence == res.highest_fence_seen
                and res.highest_fence_seen > 0
                and res.last_effect_id != request.effect_id
            ):
                metrics.inc("adapter_stale_fence_violations_total")
                emit_event(
                    "adapter.stale_fence_violation",
                    correlation_id=request.correlation_id,
                    adapter_id=self._adapter_id,
                    target_id=request.target_id,
                    fence=request.fence,
                    highest_fence_seen=res.highest_fence_seen,
                    reason="duplicate_fence_distinct_effect",
                    level="WARNING",
                )
                return AdapterApplyResponse(
                    status=AdapterApplyStatus.REJECTED_STALE_FENCE,
                    observed_version=res.current_version,
                    detail=f"Fence {request.fence} already consumed by another effect",
                )

            # 5. Resource version CAS check
            if request.expected_version != res.current_version:
                res.highest_fence_seen = max(res.highest_fence_seen, request.fence)
                return AdapterApplyResponse(
                    status=AdapterApplyStatus.REJECTED_VERSION_CONFLICT,
                    observed_version=res.current_version,
                    detail=(
                        f"Version conflict: expected {request.expected_version}, "
                        f"observed {res.current_version}"
                    ),
                )

            # 6. Validate supported action kind
            if request.action_kind not in SUPPORTED_SIMULATOR_ACTIONS:
                return AdapterApplyResponse(
                    status=AdapterApplyStatus.FAILED,
                    observed_version=res.current_version,
                    detail=f"Unsupported action_kind: {request.action_kind}",
                )

            # 7. Apply mutation atomically
            res.highest_fence_seen = request.fence
            res.version_counter += 1
            new_version = f"v{res.version_counter}"
            res.current_version = new_version
            res.last_effect_id = request.effect_id
            res.healthy = True
            res.state_data["mutation_count"] = int(res.state_data.get("mutation_count", 0)) + 1
            res.state_data["last_action_kind"] = request.action_kind
            if request.action_kind == "restart_service":
                res.state_data["service_state"] = "restarted"
            elif request.action_kind == "drain_traffic":
                res.state_data["traffic_drained"] = True
            elif request.action_kind == "scale_workers":
                res.state_data["worker_scale"] = int(request.parameters.get("scale", 4))
            elif request.action_kind == "rollback_config":
                res.state_data["config_revision"] = str(
                    request.parameters.get("revision", "rev-prev")
                )

            ext_ref = f"sim-ref-{request.target_id}-{request.fence}-{res.version_counter}"
            res.effect_log[request.effect_id] = RecordedSimulatorEffect(
                effect_id=request.effect_id,
                fence=request.fence,
                expected_version=request.expected_version,
                observed_version=new_version,
                action_kind=request.action_kind,
                action_digest=request.action_digest,
                external_ref=ext_ref,
                committed_at=now,
                status=AdapterLookupStatus.COMMITTED,
            )
            return AdapterApplyResponse(
                status=AdapterApplyStatus.APPLIED,
                external_ref=ext_ref,
                observed_version=new_version,
                detail="Applied cleanly in DisposableTargetSimulator",
            )

    async def query_effect(
        self, tenant_id: uuid.UUID, target_id: str, effect_id: str
    ) -> AdapterEffectLookupResult:
        now = self._now_fn()
        with self._lock:
            res = self._get_or_create_resource(tenant_id, target_id)
            rec = res.effect_log.get(effect_id)
            if rec is None:
                return AdapterEffectLookupResult(
                    status=AdapterLookupStatus.NOT_FOUND,
                    observed_version=res.current_version,
                    detail="Effect ID not found in simulator log",
                )
            age_sec = (now - rec.committed_at).total_seconds()
            if age_sec >= self._retention_horizon_seconds:
                metrics.inc("adapter_retention_gaps_total")
                emit_event(
                    "adapter.retention_gap",
                    adapter_id=self._adapter_id,
                    target_id=target_id,
                    effect_id=effect_id,
                    age_seconds=round(age_sec, 3),
                    retention_horizon_seconds=self._retention_horizon_seconds,
                    level="WARNING",
                )
                return AdapterEffectLookupResult(
                    status=AdapterLookupStatus.RETENTION_EXPIRED,
                    observed_version=res.current_version,
                    detail="Effect record exceeded simulator retention horizon",
                )
            return AdapterEffectLookupResult(
                status=rec.status,
                external_ref=rec.external_ref,
                observed_version=rec.observed_version,
                fence=rec.fence,
                action_digest=rec.action_digest,
                committed_at=rec.committed_at,
                detail="Effect found in simulator log",
            )

    async def read_resource(self, tenant_id: uuid.UUID, target_id: str) -> ResourceStateSnapshot:
        with self._lock:
            res = self._get_or_create_resource(tenant_id, target_id)
            return ResourceStateSnapshot(
                target_id=res.target_id,
                current_version=res.current_version,
                highest_fence_seen=res.highest_fence_seen,
                last_effect_id=res.last_effect_id,
                state_data=dict(res.state_data),
                healthy=res.healthy,
            )
