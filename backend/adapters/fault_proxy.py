"""Deterministic 12-scenario fault injection proxy for Feature E8."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from enum import StrEnum

from backend.adapters.protocol import (
    AdapterApplyRequest,
    AdapterApplyResponse,
    AdapterApplyStatus,
    AdapterCapabilityProfile,
    AdapterEffectLookupResult,
    ResourceStateSnapshot,
    TargetAdapterProtocol,
)
from backend.adapters.simulator import DisposableTargetSimulator
from backend.domain.incident import canonical_request_hash


class FaultScenario(StrEnum):
    DUPLICATE_APPLY = "DUPLICATE_APPLY"
    LOST_RESPONSE_AFTER_COMMIT = "LOST_RESPONSE_AFTER_COMMIT"
    STALE_FENCE = "STALE_FENCE"
    COMPETING_FENCES = "COMPETING_FENCES"
    EXTERNAL_VERSION_CHANGE = "EXTERNAL_VERSION_CHANGE"
    PARTIAL_EFFECT = "PARTIAL_EFFECT"
    FALSE_SUCCESS_RESPONSE = "FALSE_SUCCESS_RESPONSE"
    STATUS_LOOKUP_FAILURE = "STATUS_LOOKUP_FAILURE"
    STATUS_STORE_OUTAGE = "STATUS_STORE_OUTAGE"
    RETENTION_EXPIRY = "RETENTION_EXPIRY"
    ADAPTER_VERSION_CHANGE = "ADAPTER_VERSION_CHANGE"
    CONFORMANCE_INVALIDATION = "CONFORMANCE_INVALIDATION"


class AdapterTransportTimeoutError(Exception):
    """Raised when an adapter call times out or loses its response after reaching the wire."""


class AdapterLookupError(Exception):
    """Raised when a status lookup fails transiently."""


class AdapterUnavailableError(Exception):
    """Raised when the status lookup store is unavailable."""


@dataclass
class FaultRule:
    scenario: FaultScenario
    target_id: str | None = None
    effect_id: str | None = None
    max_triggers: int | None = None
    triggered_count: int = 0
    override_version: str | None = None
    override_code_digest: str | None = None


class FaultInjectionProxy:
    """Deterministic fault injection wrapper around any TargetAdapterProtocol."""

    def __init__(self, inner: TargetAdapterProtocol) -> None:
        self._inner = inner
        self._rules: list[FaultRule] = []

    @property
    def inner(self) -> TargetAdapterProtocol:
        return self._inner

    def set_rule(self, rule: FaultRule) -> None:
        self._rules.append(rule)

    def clear_rules(self) -> None:
        self._rules.clear()

    def _match_rule(
        self,
        scenario: FaultScenario,
        target_id: str | None = None,
        effect_id: str | None = None,
    ) -> FaultRule | None:
        for rule in self._rules:
            if rule.scenario != scenario:
                continue
            if rule.target_id is not None and target_id is not None and rule.target_id != target_id:
                continue
            if rule.effect_id is not None and effect_id is not None and rule.effect_id != effect_id:
                continue
            if rule.max_triggers is not None and rule.triggered_count >= rule.max_triggers:
                continue
            rule.triggered_count += 1
            return rule
        return None

    def profile(self) -> AdapterCapabilityProfile:
        base = self._inner.profile()
        ver_rule = self._match_rule(FaultScenario.ADAPTER_VERSION_CHANGE)
        if ver_rule is not None:
            new_ver = ver_rule.override_version or f"{base.version}-drifted"
            new_digest = ver_rule.override_code_digest or canonical_request_hash(
                {
                    "base_digest": base.code_digest,
                    "override_version": new_ver,
                }
            )
            return replace(base, version=new_ver, code_digest=new_digest)
        return base

    async def apply(self, request: AdapterApplyRequest) -> AdapterApplyResponse:
        # Scenario 5: External version change before apply
        if self._match_rule(
            FaultScenario.EXTERNAL_VERSION_CHANGE,
            target_id=request.target_id,
            effect_id=request.effect_id,
        ) is not None and isinstance(self._inner, DisposableTargetSimulator):
            self._inner.advance_version_out_of_band(request.tenant_id, request.target_id)

        # Scenario 3: Stale fence injection (force fence lower than highest_fence_seen)
        if (
            self._match_rule(
                FaultScenario.STALE_FENCE,
                target_id=request.target_id,
                effect_id=request.effect_id,
            )
            is not None
        ):
            snap = await self._inner.read_resource(request.tenant_id, request.target_id)
            if snap.highest_fence_seen == 0 and isinstance(self._inner, DisposableTargetSimulator):
                # Advance fence via a dummy higher fence first or force fence=0
                stale_req = replace(request, fence=0)
            else:
                stale_req = replace(request, fence=max(0, snap.highest_fence_seen - 1))
            return await self._inner.apply(stale_req)

        # Scenario 6: Partial effect
        if self._match_rule(
            FaultScenario.PARTIAL_EFFECT,
            target_id=request.target_id,
            effect_id=request.effect_id,
        ) is not None and isinstance(self._inner, DisposableTargetSimulator):
            return self._inner.record_partial_effect(request)

        # Scenario 7: False success response (do NOT call underlying apply!)
        if (
            self._match_rule(
                FaultScenario.FALSE_SUCCESS_RESPONSE,
                target_id=request.target_id,
                effect_id=request.effect_id,
            )
            is not None
        ):
            return AdapterApplyResponse(
                status=AdapterApplyStatus.APPLIED,
                external_ref=f"false-success-ref-{request.effect_id[:8]}",
                observed_version="v2",
                detail="Simulated false success without mutating resource",
            )

        # Scenario 1: Duplicate apply (deliver twice to underlying adapter)
        if (
            self._match_rule(
                FaultScenario.DUPLICATE_APPLY,
                target_id=request.target_id,
                effect_id=request.effect_id,
            )
            is not None
        ):
            await self._inner.apply(request)
            return await self._inner.apply(request)

        # Scenario 2: Lost response after commit
        if (
            self._match_rule(
                FaultScenario.LOST_RESPONSE_AFTER_COMMIT,
                target_id=request.target_id,
                effect_id=request.effect_id,
            )
            is not None
        ):
            await self._inner.apply(request)
            raise AdapterTransportTimeoutError(
                f"Simulated lost response after commit for effect_id={request.effect_id}"
            )

        return await self._inner.apply(request)

    async def query_effect(
        self, tenant_id: uuid.UUID, target_id: str, effect_id: str
    ) -> AdapterEffectLookupResult:
        # Scenario 9: Status-store outage
        if (
            self._match_rule(
                FaultScenario.STATUS_STORE_OUTAGE,
                target_id=target_id,
                effect_id=effect_id,
            )
            is not None
        ):
            raise AdapterUnavailableError(f"Simulated status-store outage for target={target_id}")

        # Scenario 8: Transient status lookup failure
        if (
            self._match_rule(
                FaultScenario.STATUS_LOOKUP_FAILURE,
                target_id=target_id,
                effect_id=effect_id,
            )
            is not None
        ):
            raise AdapterLookupError(f"Simulated status lookup failure for effect_id={effect_id}")

        return await self._inner.query_effect(tenant_id, target_id, effect_id)

    async def read_resource(self, tenant_id: uuid.UUID, target_id: str) -> ResourceStateSnapshot:
        return await self._inner.read_resource(tenant_id, target_id)
