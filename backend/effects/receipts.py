"""Feature B3 — Effect Receipt and Reconciliation, ActionGateway, and Postcondition Verifier."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.adapters.conformance import MutationCapabilityGate
from backend.adapters.fault_proxy import (
    AdapterLookupError,
    AdapterTransportTimeoutError,
    AdapterUnavailableError,
)
from backend.adapters.protocol import (
    AdapterApplyRequest,
    AdapterApplyStatus,
    AdapterLookupStatus,
    TargetAdapterProtocol,
)
from backend.domain.incident import (
    DegradedRecoveryGate,
    DomainError,
    canonical_request_hash,
)
from backend.effects.fencing import (
    ApprovedPlanContext,
    MutationPermit,
    ReservationState,
    ResourceReservationManager,
    verify_approved_plan_context,
)
from backend.observability.metrics import emit_event, metrics
from backend.persistence.models import (
    EffectReceipt,
    ResourceEpoch,
    ResourceReservation,
)


class EffectState(StrEnum):
    PREPARED = "PREPARED"
    SENT = "SENT"
    UNKNOWN = "UNKNOWN"
    CONFIRMED = "CONFIRMED"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    NEEDS_REVIEW = "NEEDS_REVIEW"


TERMINAL_EFFECT_STATES: frozenset[EffectState] = frozenset(
    {
        EffectState.VERIFIED,
        EffectState.FAILED,
        EffectState.NEEDS_REVIEW,
    }
)

ALLOWED_EFFECT_TRANSITIONS: dict[EffectState, frozenset[EffectState]] = {
    EffectState.PREPARED: frozenset(
        {
            EffectState.SENT,
            EffectState.FAILED,
            EffectState.NEEDS_REVIEW,
        }
    ),
    EffectState.SENT: frozenset(
        {
            EffectState.CONFIRMED,
            EffectState.UNKNOWN,
            EffectState.FAILED,
            EffectState.NEEDS_REVIEW,
        }
    ),
    EffectState.UNKNOWN: frozenset(
        {
            EffectState.CONFIRMED,
            EffectState.FAILED,
            EffectState.NEEDS_REVIEW,
        }
    ),
    EffectState.CONFIRMED: frozenset(
        {
            EffectState.VERIFIED,
            EffectState.FAILED,
            EffectState.NEEDS_REVIEW,
        }
    ),
    EffectState.VERIFIED: frozenset(),
    EffectState.FAILED: frozenset(),
    EffectState.NEEDS_REVIEW: frozenset(),
}


class EffectReceiptError(DomainError):
    """Base error for B3 effect receipt and reconciliation failures."""


class InvalidEffectStateTransitionError(EffectReceiptError):
    def __init__(self, current: EffectState, target: EffectState, reason: str = "") -> None:
        self.current = current
        self.target = target
        msg = f"Illegal effect receipt state transition from {current} to {target}"
        if reason:
            msg = f"{msg}: {reason}"
        super().__init__(msg)


class StaleEffectClaimError(EffectReceiptError):
    def __init__(self, receipt_id: uuid.UUID) -> None:
        self.receipt_id = receipt_id
        super().__init__(f"Stale claim_token or state on effect_receipt {receipt_id}")


class EffectIdempotencyConflictError(EffectReceiptError):
    def __init__(self, idempotency_key: str) -> None:
        self.idempotency_key = idempotency_key
        super().__init__(
            f"Effect idempotency key '{idempotency_key}' already exists with conflicting payload"
        )


def validate_effect_transition(
    current: EffectState,
    target: EffectState,
    *,
    allow_controlled_retry: bool = False,
) -> bool:
    if current == target:
        return False
    if current in TERMINAL_EFFECT_STATES:
        raise InvalidEffectStateTransitionError(
            current, target, "cannot transition out of terminal state"
        )
    if current == EffectState.UNKNOWN and target == EffectState.SENT and allow_controlled_retry:
        return True
    allowed = ALLOWED_EFFECT_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise InvalidEffectStateTransitionError(current, target)
    return True


def compute_effect_idempotency_key(
    *,
    tenant_id: uuid.UUID,
    incident_id: uuid.UUID,
    plan_digest: str,
    target_id: str,
    adapter_id: str,
    action_digest: str,
    expected_version: str,
) -> str:
    """Compute the deterministic B3 effect receipt idempotency key."""
    return canonical_request_hash(
        {
            "tenant_id": str(tenant_id),
            "incident_id": str(incident_id),
            "plan_digest": plan_digest,
            "target_id": target_id,
            "adapter_id": adapter_id,
            "action_digest": action_digest,
            "expected_version": expected_version,
        }
    )


@dataclass(frozen=True)
class ActionSpec:
    action_kind: str
    parameters: dict[str, Any] = field(default_factory=dict)

    def action_digest(self) -> str:
        return canonical_request_hash(
            {
                "action_kind": self.action_kind,
                "parameters": self.parameters,
            }
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "action_kind": self.action_kind,
            "parameters": dict(self.parameters),
        }


@dataclass(frozen=True)
class EffectReceiptSnapshot:
    id: uuid.UUID
    tenant_id: uuid.UUID
    incident_id: uuid.UUID
    plan_digest: str
    target_id: str
    adapter_id: str
    adapter_digest: str
    idempotency_key: str
    fence: int
    expected_version: str
    state: EffectState
    attempt_count: int
    external_ref: str | None
    observed_version: str | None
    action_digest: str
    action_payload: dict[str, Any] | None
    claim_token: uuid.UUID | None
    lease_owner: str | None
    lease_expires_at: datetime | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class PostconditionVerificationResult:
    verified: bool
    false_success_detected: bool
    observed_version: str | None
    reason: str


class IndependentPostconditionVerifier:
    """Independently inspects target resource state via `read_resource` before VERIFIED."""

    async def verify(
        self,
        *,
        adapter: TargetAdapterProtocol,
        receipt: EffectReceiptSnapshot,
        correlation_id: str = "",
    ) -> PostconditionVerificationResult:
        snapshot = await adapter.read_resource(receipt.tenant_id, receipt.target_id)

        if not snapshot.healthy:
            metrics.inc("verifier_false_success_total")
            metrics.inc("adapter_lookup_mismatches_total")
            emit_event(
                "verifier.false_success",
                incident_id=receipt.incident_id,
                correlation_id=correlation_id,
                target_id=receipt.target_id,
                adapter_id=receipt.adapter_id,
                effect_id=receipt.idempotency_key,
                reason="target_unhealthy_after_confirm",
                level="ERROR",
            )
            emit_event(
                "adapter.lookup_mismatch",
                incident_id=receipt.incident_id,
                correlation_id=correlation_id,
                target_id=receipt.target_id,
                adapter_id=receipt.adapter_id,
                effect_id=receipt.idempotency_key,
                reason="target_unhealthy_after_confirm",
                level="ERROR",
            )
            return PostconditionVerificationResult(
                verified=False,
                false_success_detected=True,
                observed_version=snapshot.current_version,
                reason="Target resource reported unhealthy after confirmed effect",
            )

        if (
            receipt.observed_version is None
            or snapshot.current_version != receipt.observed_version
            or snapshot.current_version == receipt.expected_version
            or snapshot.highest_fence_seen < receipt.fence
            or snapshot.last_effect_id != receipt.idempotency_key
        ):
            metrics.inc("verifier_false_success_total")
            metrics.inc("adapter_lookup_mismatches_total")
            emit_event(
                "verifier.false_success",
                incident_id=receipt.incident_id,
                correlation_id=correlation_id,
                target_id=receipt.target_id,
                adapter_id=receipt.adapter_id,
                effect_id=receipt.idempotency_key,
                claimed_version=receipt.observed_version,
                actual_version=snapshot.current_version,
                expected_version=receipt.expected_version,
                highest_fence_seen=snapshot.highest_fence_seen,
                receipt_fence=receipt.fence,
                last_effect_id=snapshot.last_effect_id,
                level="ERROR",
            )
            emit_event(
                "adapter.lookup_mismatch",
                incident_id=receipt.incident_id,
                correlation_id=correlation_id,
                target_id=receipt.target_id,
                adapter_id=receipt.adapter_id,
                effect_id=receipt.idempotency_key,
                claimed_version=receipt.observed_version,
                actual_version=snapshot.current_version,
                level="ERROR",
            )
            return PostconditionVerificationResult(
                verified=False,
                false_success_detected=True,
                observed_version=snapshot.current_version,
                reason=(
                    f"Postcondition mismatch: claimed={receipt.observed_version}, "
                    f"actual={snapshot.current_version}, "
                    f"last_effect_id={snapshot.last_effect_id}"
                ),
            )

        return PostconditionVerificationResult(
            verified=True,
            false_success_detected=False,
            observed_version=snapshot.current_version,
            reason="Postcondition verified independently via read_resource",
        )


def _to_receipt_snapshot(row: EffectReceipt) -> EffectReceiptSnapshot:
    return EffectReceiptSnapshot(
        id=row.id,
        tenant_id=row.tenant_id,
        incident_id=row.incident_id,
        plan_digest=row.plan_digest,
        target_id=row.target_id,
        adapter_id=row.adapter_id,
        adapter_digest=row.adapter_digest,
        idempotency_key=row.idempotency_key,
        fence=row.fence,
        expected_version=row.expected_version,
        state=EffectState(row.state),
        attempt_count=row.attempt_count,
        external_ref=row.external_ref,
        observed_version=row.observed_version,
        action_digest=row.action_digest,
        action_payload=dict(row.action_payload) if row.action_payload else None,
        claim_token=row.claim_token,
        lease_owner=row.lease_owner,
        lease_expires_at=row.lease_expires_at,
        last_error=row.last_error,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class ActionGateway:
    """Controlled B3 action execution gateway enforcing P2 approval, E8 conformance, B2 fencing,
    receipt-before-call persistence, and independent postcondition verification.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        verifier: IndependentPostconditionVerifier | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._reservation_mgr = ResourceReservationManager(session_factory)
        self._capability_gate = MutationCapabilityGate(session_factory)
        self._verifier = verifier or IndependentPostconditionVerifier()

    async def get_receipt(
        self, tenant_id: uuid.UUID, receipt_id: uuid.UUID
    ) -> EffectReceiptSnapshot | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(EffectReceipt).where(
                        EffectReceipt.tenant_id == tenant_id,
                        EffectReceipt.id == receipt_id,
                    )
                )
            ).scalar_one_or_none()
            return _to_receipt_snapshot(row) if row is not None else None

    async def execute_with_permit(
        self,
        *,
        permit: MutationPermit,
        approval: ApprovedPlanContext | None,
        action: ActionSpec,
        adapter: TargetAdapterProtocol,
        lease_seconds: float = 30.0,
        verify_immediately: bool = True,
        correlation_id: str = "",
        now: datetime | None = None,
    ) -> EffectReceiptSnapshot:
        """Execute an approved, version-fenced action through the B3 receipt lifecycle."""
        DegradedRecoveryGate.check_mutation_allowed()
        current_time = now or datetime.now(UTC)
        prof = adapter.profile()
        action_digest = action.action_digest()

        # 1. Re-verify fail-closed P2 approval context
        verify_approved_plan_context(
            approval,
            tenant_id=permit.tenant_id,
            incident_id=permit.incident_id,
            target_id=permit.target_id,
            plan_digest=permit.plan_digest,
            action_digest=action_digest,
            expected_version=permit.expected_version,
            is_simulator_adapter=prof.is_simulator,
            now=current_time,
        )

        # 2. Re-verify E8 adapter conformance & exact code_digest
        conformance = await self._capability_gate.verify_mutation_allowed(
            adapter, correlation_id=correlation_id, now=current_time
        )

        idempotency_key = compute_effect_idempotency_key(
            tenant_id=permit.tenant_id,
            incident_id=permit.incident_id,
            plan_digest=permit.plan_digest,
            target_id=permit.target_id,
            adapter_id=prof.adapter_id,
            action_digest=action_digest,
            expected_version=permit.expected_version,
        )

        # 3. Short Tx 1: Check existing receipt or atomically persist PREPARED -> SENT + B2 DISPATCHED
        claim_token = uuid.uuid4()
        lease_until = current_time + timedelta(seconds=lease_seconds)

        async with self._session_factory() as session:
            existing = (
                await session.execute(
                    select(EffectReceipt)
                    .where(
                        EffectReceipt.tenant_id == permit.tenant_id,
                        EffectReceipt.idempotency_key == idempotency_key,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()

            if existing is not None:
                if (
                    existing.action_digest != action_digest
                    or existing.target_id != permit.target_id
                    or existing.expected_version != permit.expected_version
                ):
                    metrics.inc("effect_duplicate_rejects_total")
                    emit_event(
                        "effect.duplicate_reject",
                        incident_id=permit.incident_id,
                        correlation_id=correlation_id,
                        idempotency_key=idempotency_key,
                        reason="payload_mismatch",
                        level="WARNING",
                    )
                    raise EffectIdempotencyConflictError(idempotency_key)

                cur_state = EffectState(existing.state)
                if cur_state in TERMINAL_EFFECT_STATES or cur_state in (
                    EffectState.CONFIRMED,
                    EffectState.UNKNOWN,
                ):
                    metrics.inc("effect_duplicate_rejects_total")
                    emit_event(
                        "effect.duplicate_reject",
                        incident_id=permit.incident_id,
                        correlation_id=correlation_id,
                        idempotency_key=idempotency_key,
                        existing_state=cur_state.value,
                    )
                    return _to_receipt_snapshot(existing)

                if (
                    cur_state == EffectState.SENT
                    and existing.lease_expires_at is not None
                    and existing.lease_expires_at > current_time
                ):
                    metrics.inc("effect_duplicate_rejects_total")
                    emit_event(
                        "effect.duplicate_reject",
                        incident_id=permit.incident_id,
                        correlation_id=correlation_id,
                        idempotency_key=idempotency_key,
                        existing_state=cur_state.value,
                    )
                    return _to_receipt_snapshot(existing)

                # Transition PREPARED -> SENT alongside B2 RESERVED -> DISPATCHED
                await self._reservation_mgr.mark_dispatched(
                    permit,
                    session=session,
                    extend_lease_seconds=lease_seconds,
                    correlation_id=correlation_id,
                    now=current_time,
                )
                validate_effect_transition(cur_state, EffectState.SENT)
                existing.state = EffectState.SENT.value
                existing.attempt_count += 1
                existing.claim_token = claim_token
                existing.lease_owner = "action-gateway"
                existing.lease_expires_at = lease_until
                existing.adapter_digest = conformance.code_digest
                existing.updated_at = current_time
                await session.commit()
                receipt_id = existing.id
            else:
                # Validate & mark B2 reservation DISPATCHED in the same transaction
                await self._reservation_mgr.mark_dispatched(
                    permit,
                    session=session,
                    extend_lease_seconds=lease_seconds,
                    correlation_id=correlation_id,
                    now=current_time,
                )
                receipt_id = uuid.uuid4()
                new_receipt = EffectReceipt(
                    id=receipt_id,
                    tenant_id=permit.tenant_id,
                    incident_id=permit.incident_id,
                    plan_digest=permit.plan_digest,
                    target_id=permit.target_id,
                    adapter_id=prof.adapter_id,
                    adapter_digest=conformance.code_digest,
                    idempotency_key=idempotency_key,
                    fence=permit.fence,
                    expected_version=permit.expected_version,
                    state=EffectState.PREPARED.value,
                    attempt_count=0,
                    action_digest=action_digest,
                    action_payload=action.to_payload(),
                    created_at=current_time,
                    updated_at=current_time,
                )
                session.add(new_receipt)
                try:
                    await session.flush()
                except IntegrityError:
                    await session.rollback()
                    async with self._session_factory() as retry_session:
                        raced = (
                            await retry_session.execute(
                                select(EffectReceipt).where(
                                    EffectReceipt.tenant_id == permit.tenant_id,
                                    EffectReceipt.idempotency_key == idempotency_key,
                                )
                            )
                        ).scalar_one()
                        return _to_receipt_snapshot(raced)

                metrics.inc("effect_prepared_total")
                validate_effect_transition(EffectState.PREPARED, EffectState.SENT)
                new_receipt.state = EffectState.SENT.value
                new_receipt.attempt_count = 1
                new_receipt.claim_token = claim_token
                new_receipt.lease_owner = "action-gateway"
                new_receipt.lease_expires_at = lease_until
                await session.commit()
                metrics.inc("effect_sent_total")

        # 4. External Adapter Call (NO database transaction held open!)
        apply_req = AdapterApplyRequest(
            tenant_id=permit.tenant_id,
            target_id=permit.target_id,
            effect_id=idempotency_key,
            fence=permit.fence,
            expected_version=permit.expected_version,
            action_kind=action.action_kind,
            action_digest=action_digest,
            parameters=action.parameters,
            correlation_id=correlation_id,
        )

        try:
            apply_resp = await adapter.apply(apply_req)
        except (AdapterTransportTimeoutError, TimeoutError, ConnectionError) as exc:
            # Ambiguous external outcome -> transition SENT -> UNKNOWN and hold B2 reservation for reconciliation
            return await self._transition_sent_to_unknown(
                tenant_id=permit.tenant_id,
                receipt_id=receipt_id,
                claim_token=claim_token,
                permit=permit,
                error_detail=str(exc),
                correlation_id=correlation_id,
                now=current_time,
            )

        # 5. Short Tx 2: Record apply outcome based on AdapterApplyStatus
        if apply_resp.status in (
            AdapterApplyStatus.APPLIED,
            AdapterApplyStatus.DUPLICATE_APPLIED,
        ):
            confirmed_snap = await self._transition_to_confirmed(
                tenant_id=permit.tenant_id,
                receipt_id=receipt_id,
                claim_token=claim_token,
                observed_version=apply_resp.observed_version,
                external_ref=apply_resp.external_ref,
                now=current_time,
            )
            if not verify_immediately:
                return confirmed_snap
            return await self.verify_and_finalize_confirmed_receipt(
                receipt_id=confirmed_snap.id,
                tenant_id=permit.tenant_id,
                permit=permit,
                adapter=adapter,
                claim_token=claim_token,
                correlation_id=correlation_id,
                now=current_time,
            )

        if apply_resp.status == AdapterApplyStatus.PARTIAL_FAILURE:
            return await self._finalize_non_confirmed(
                tenant_id=permit.tenant_id,
                receipt_id=receipt_id,
                claim_token=claim_token,
                permit=permit,
                target_state=EffectState.NEEDS_REVIEW,
                observed_version=None,
                external_ref=apply_resp.external_ref,
                error_detail=apply_resp.detail,
                now=current_time,
            )

        # REJECTED_STALE_FENCE, REJECTED_VERSION_CONFLICT, or FAILED -> FAILED
        if apply_resp.status == AdapterApplyStatus.REJECTED_STALE_FENCE:
            metrics.inc("fence_stale_rejects_total")
            emit_event(
                "fence.stale_reject",
                incident_id=permit.incident_id,
                correlation_id=correlation_id,
                target_id=permit.target_id,
                fence=permit.fence,
                reason="rejected_by_adapter",
                level="WARNING",
            )
        elif apply_resp.status == AdapterApplyStatus.REJECTED_VERSION_CONFLICT:
            metrics.inc("target_version_conflicts_total")
            emit_event(
                "target.version_conflict",
                incident_id=permit.incident_id,
                correlation_id=correlation_id,
                target_id=permit.target_id,
                expected_version=permit.expected_version,
                observed_version=apply_resp.observed_version,
                level="WARNING",
            )

        return await self._finalize_non_confirmed(
            tenant_id=permit.tenant_id,
            receipt_id=receipt_id,
            claim_token=claim_token,
            permit=permit,
            target_state=EffectState.FAILED,
            observed_version=(
                apply_resp.observed_version
                if apply_resp.status == AdapterApplyStatus.REJECTED_VERSION_CONFLICT
                else None
            ),
            external_ref=apply_resp.external_ref,
            error_detail=apply_resp.detail,
            now=current_time,
        )

    async def verify_and_finalize_confirmed_receipt(
        self,
        *,
        receipt_id: uuid.UUID,
        tenant_id: uuid.UUID,
        permit: MutationPermit | None,
        adapter: TargetAdapterProtocol,
        claim_token: uuid.UUID | None = None,
        correlation_id: str = "",
        now: datetime | None = None,
    ) -> EffectReceiptSnapshot:
        """Run IndependentPostconditionVerifier on a CONFIRMED receipt and transition to
        VERIFIED or NEEDS_REVIEW while releasing the B2 reservation.
        """
        current_time = now or datetime.now(UTC)
        receipt = await self.get_receipt(tenant_id, receipt_id)
        if receipt is None:
            raise StaleEffectClaimError(receipt_id)
        if receipt.state in TERMINAL_EFFECT_STATES:
            return receipt

        # Run independent read_resource verification OUTSIDE any DB transaction
        ver_res = await self._verifier.verify(
            adapter=adapter,
            receipt=receipt,
            correlation_id=correlation_id,
        )

        if ver_res.false_success_detected:
            await self._capability_gate.quarantine_adapter(
                adapter,
                reason=ver_res.reason,
                correlation_id=correlation_id,
                now=current_time,
            )

        target_state = EffectState.VERIFIED if ver_res.verified else EffectState.NEEDS_REVIEW
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(EffectReceipt)
                    .where(
                        EffectReceipt.id == receipt_id,
                        EffectReceipt.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                raise StaleEffectClaimError(receipt_id)
            if claim_token is not None and row.claim_token != claim_token:
                raise StaleEffectClaimError(receipt_id)

            cur_state = EffectState(row.state)
            if cur_state in TERMINAL_EFFECT_STATES:
                return _to_receipt_snapshot(row)

            validate_effect_transition(cur_state, target_state)
            row.state = target_state.value
            row.updated_at = current_time
            row.lease_expires_at = None
            if not ver_res.verified:
                row.last_error = ver_res.reason

            await self._release_reservation_for_receipt(
                session,
                row,
                permit=permit,
                observed_version=row.observed_version if ver_res.verified else None,
            )
            await session.commit()

            if target_state == EffectState.VERIFIED:
                metrics.inc("effect_verified_total")
            else:
                metrics.inc("effect_needs_review_total")

            return _to_receipt_snapshot(row)

    async def _transition_sent_to_unknown(
        self,
        *,
        tenant_id: uuid.UUID,
        receipt_id: uuid.UUID,
        claim_token: uuid.UUID,
        permit: MutationPermit,
        error_detail: str,
        correlation_id: str,
        now: datetime,
    ) -> EffectReceiptSnapshot:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(EffectReceipt)
                    .where(
                        EffectReceipt.id == receipt_id,
                        EffectReceipt.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None or row.claim_token != claim_token:
                raise StaleEffectClaimError(receipt_id)

            cur_state = EffectState(row.state)
            validate_effect_transition(cur_state, EffectState.UNKNOWN)
            row.state = EffectState.UNKNOWN.value
            row.last_error = error_detail
            row.lease_expires_at = None
            row.updated_at = now

            # Extend the B2 DISPATCHED reservation lease so no competing reservation steals the target
            res = (
                await session.execute(
                    select(ResourceReservation)
                    .where(
                        ResourceReservation.id == permit.reservation_id,
                        ResourceReservation.tenant_id == permit.tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if res is not None and res.state == ReservationState.DISPATCHED.value:
                res.expires_at = max(res.expires_at, now + timedelta(seconds=60))

            await session.commit()
            age_sec = max(0.0, (now - row.created_at).total_seconds())
            metrics.inc("effect_unknown_total")
            metrics.set_gauge("effect_unknown_age_seconds", age_sec)
            emit_event(
                "effect.unknown_age",
                incident_id=row.incident_id,
                correlation_id=correlation_id,
                target_id=row.target_id,
                adapter_id=row.adapter_id,
                effect_id=row.idempotency_key,
                unknown_age_seconds=round(age_sec, 3),
                error=error_detail,
                level="WARNING",
            )
            return _to_receipt_snapshot(row)

    async def _transition_to_confirmed(
        self,
        *,
        tenant_id: uuid.UUID,
        receipt_id: uuid.UUID,
        claim_token: uuid.UUID,
        observed_version: str | None,
        external_ref: str | None,
        now: datetime,
    ) -> EffectReceiptSnapshot:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(EffectReceipt)
                    .where(
                        EffectReceipt.id == receipt_id,
                        EffectReceipt.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None or row.claim_token != claim_token:
                raise StaleEffectClaimError(receipt_id)

            cur_state = EffectState(row.state)
            validate_effect_transition(cur_state, EffectState.CONFIRMED)
            row.state = EffectState.CONFIRMED.value
            row.observed_version = observed_version
            row.external_ref = external_ref
            row.updated_at = now
            await session.commit()
            metrics.inc("effect_confirmed_total")
            return _to_receipt_snapshot(row)

    async def _finalize_non_confirmed(
        self,
        *,
        tenant_id: uuid.UUID,
        receipt_id: uuid.UUID,
        claim_token: uuid.UUID,
        permit: MutationPermit | None,
        target_state: EffectState,
        observed_version: str | None,
        external_ref: str | None,
        error_detail: str,
        now: datetime,
    ) -> EffectReceiptSnapshot:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(EffectReceipt)
                    .where(
                        EffectReceipt.id == receipt_id,
                        EffectReceipt.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None or row.claim_token != claim_token:
                raise StaleEffectClaimError(receipt_id)

            cur_state = EffectState(row.state)
            validate_effect_transition(cur_state, target_state)
            row.state = target_state.value
            if external_ref is not None:
                row.external_ref = external_ref
            if observed_version is not None:
                row.observed_version = observed_version
            row.last_error = error_detail
            row.lease_expires_at = None
            row.updated_at = now

            await self._release_reservation_for_receipt(
                session,
                row,
                permit=permit,
                observed_version=observed_version,
            )
            await session.commit()
            if target_state == EffectState.FAILED:
                metrics.inc("effect_failed_total")
            elif target_state == EffectState.NEEDS_REVIEW:
                metrics.inc("effect_needs_review_total")
            return _to_receipt_snapshot(row)

    async def _release_reservation_for_receipt(
        self,
        session: AsyncSession,
        row: EffectReceipt,
        *,
        permit: MutationPermit | None,
        observed_version: str | None,
    ) -> None:
        if permit is not None:
            await self._reservation_mgr.release_reservation(
                permit, observed_version=observed_version, session=session
            )
            return

        epoch = (
            await session.execute(
                select(ResourceEpoch)
                .where(
                    ResourceEpoch.tenant_id == row.tenant_id,
                    ResourceEpoch.target_id == row.target_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()

        res = (
            await session.execute(
                select(ResourceReservation)
                .where(
                    ResourceReservation.tenant_id == row.tenant_id,
                    ResourceReservation.target_id == row.target_id,
                    ResourceReservation.fence == row.fence,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()

        if res is not None and res.state in (
            ReservationState.RESERVED.value,
            ReservationState.DISPATCHED.value,
        ):
            res.state = ReservationState.RELEASED.value

        if epoch is not None and observed_version is not None and row.fence >= epoch.latest_fence:
            epoch.version_seen = observed_version


class EffectReconciler:
    """Short-transaction B3 effect reconciler resolving UNKNOWN and lease-expired SENT receipts."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        max_reconcile_attempts: int = 3,
        max_apply_attempts: int = 2,
        verifier: IndependentPostconditionVerifier | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._max_reconcile_attempts = max_reconcile_attempts
        self._max_apply_attempts = max_apply_attempts
        self._gateway = ActionGateway(session_factory, verifier=verifier)

    async def reconcile_pending(
        self,
        *,
        adapters: dict[str, TargetAdapterProtocol],
        tenant_id: uuid.UUID | None = None,
        incident_id: uuid.UUID | None = None,
        batch_size: int = 10,
        lease_seconds: float = 15.0,
        allow_controlled_retry: bool = True,
        correlation_id: str = "",
        now: datetime | None = None,
    ) -> list[EffectReceiptSnapshot]:
        """Claim eligible UNKNOWN or lease-expired SENT receipts using FOR UPDATE SKIP LOCKED
        and reconcile each outside the DB transaction.
        """
        claim_time = now or datetime.now(UTC)
        lease_until = claim_time + timedelta(seconds=lease_seconds)

        claimed_items: list[tuple[EffectReceiptSnapshot, uuid.UUID]] = []

        async with self._session_factory() as session:
            stmt = (
                select(EffectReceipt)
                .where(
                    EffectReceipt.state.in_([EffectState.UNKNOWN.value, EffectState.SENT.value]),
                    or_(
                        EffectReceipt.lease_expires_at.is_(None),
                        EffectReceipt.lease_expires_at <= claim_time,
                    ),
                )
                .order_by(EffectReceipt.updated_at.asc())
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )
            if tenant_id is not None:
                stmt = stmt.where(EffectReceipt.tenant_id == tenant_id)
            if incident_id is not None:
                stmt = stmt.where(EffectReceipt.incident_id == incident_id)

            rows = (await session.execute(stmt)).scalars().all()
            if not rows:
                return []

            for row in rows:
                new_token = uuid.uuid4()
                cur_state = EffectState(row.state)
                if cur_state == EffectState.SENT:
                    # Worker lease expired while SENT -> transition SENT -> UNKNOWN first
                    validate_effect_transition(EffectState.SENT, EffectState.UNKNOWN)
                    row.state = EffectState.UNKNOWN.value
                    metrics.inc("effect_unknown_total")

                age_sec = max(0.0, (claim_time - row.created_at).total_seconds())
                metrics.set_gauge("effect_unknown_age_seconds", age_sec)
                emit_event(
                    "effect.unknown_age",
                    incident_id=row.incident_id,
                    correlation_id=correlation_id,
                    target_id=row.target_id,
                    adapter_id=row.adapter_id,
                    effect_id=row.idempotency_key,
                    unknown_age_seconds=round(age_sec, 3),
                )

                row.claim_token = new_token
                row.lease_owner = "effect-reconciler"
                row.lease_expires_at = lease_until
                row.updated_at = claim_time
                claimed_items.append((_to_receipt_snapshot(row), new_token))

            await session.commit()

        results: list[EffectReceiptSnapshot] = []
        for snap, token in claimed_items:
            resolved = await self._reconcile_single(
                snap=snap,
                claim_token=token,
                adapters=adapters,
                allow_controlled_retry=allow_controlled_retry,
                correlation_id=correlation_id,
                now=claim_time,
            )
            results.append(resolved)
        return results

    async def _reconcile_single(
        self,
        *,
        snap: EffectReceiptSnapshot,
        claim_token: uuid.UUID,
        adapters: dict[str, TargetAdapterProtocol],
        allow_controlled_retry: bool = True,
        correlation_id: str,
        now: datetime,
    ) -> EffectReceiptSnapshot:
        metrics.inc("reconciliation_attempts_total")
        emit_event(
            "reconciliation.attempt",
            incident_id=snap.incident_id,
            correlation_id=correlation_id,
            target_id=snap.target_id,
            adapter_id=snap.adapter_id,
            effect_id=snap.idempotency_key,
            attempt_count=snap.attempt_count + 1,
        )

        adapter = adapters.get(snap.adapter_id)
        if adapter is None:
            return await self._gateway._finalize_non_confirmed(
                tenant_id=snap.tenant_id,
                receipt_id=snap.id,
                claim_token=claim_token,
                permit=None,
                target_state=EffectState.NEEDS_REVIEW,
                observed_version=None,
                external_ref=None,
                error_detail=f"Adapter '{snap.adapter_id}' not registered for reconciliation",
                now=now,
            )

        prof = adapter.profile()
        age_sec = max(0.0, (now - snap.created_at).total_seconds())

        # 1. Client-side retention horizon check before trusting lookup
        if age_sec >= prof.retention_horizon_seconds:
            metrics.inc("adapter_retention_gaps_total")
            emit_event(
                "adapter.retention_gap",
                incident_id=snap.incident_id,
                correlation_id=correlation_id,
                target_id=snap.target_id,
                adapter_id=snap.adapter_id,
                effect_id=snap.idempotency_key,
                age_seconds=round(age_sec, 3),
                retention_horizon_seconds=prof.retention_horizon_seconds,
                level="WARNING",
            )
            return await self._gateway._finalize_non_confirmed(
                tenant_id=snap.tenant_id,
                receipt_id=snap.id,
                claim_token=claim_token,
                permit=None,
                target_state=EffectState.NEEDS_REVIEW,
                observed_version=None,
                external_ref=None,
                error_detail="Effect age exceeded adapter retention horizon while UNKNOWN",
                now=now,
            )

        # 2. Query adapter status outside DB transaction
        try:
            lookup = await adapter.query_effect(
                snap.tenant_id, snap.target_id, snap.idempotency_key
            )
        except (AdapterLookupError, AdapterUnavailableError, TimeoutError, ConnectionError) as exc:
            return await self._handle_lookup_failure(
                snap=snap,
                claim_token=claim_token,
                error_detail=str(exc),
                now=now,
            )

        # 3. Evaluate lookup result
        if lookup.status == AdapterLookupStatus.RETENTION_EXPIRED:
            return await self._gateway._finalize_non_confirmed(
                tenant_id=snap.tenant_id,
                receipt_id=snap.id,
                claim_token=claim_token,
                permit=None,
                target_state=EffectState.NEEDS_REVIEW,
                observed_version=None,
                external_ref=None,
                error_detail=lookup.detail or "Adapter reported retention expired",
                now=now,
            )

        if lookup.status == AdapterLookupStatus.COMMITTED:
            confirmed = await self._gateway._transition_to_confirmed(
                tenant_id=snap.tenant_id,
                receipt_id=snap.id,
                claim_token=claim_token,
                observed_version=lookup.observed_version,
                external_ref=lookup.external_ref,
                now=now,
            )
            return await self._gateway.verify_and_finalize_confirmed_receipt(
                receipt_id=confirmed.id,
                tenant_id=snap.tenant_id,
                permit=None,
                adapter=adapter,
                claim_token=claim_token,
                correlation_id=correlation_id,
                now=now,
            )

        if lookup.status == AdapterLookupStatus.PARTIAL:
            return await self._gateway._finalize_non_confirmed(
                tenant_id=snap.tenant_id,
                receipt_id=snap.id,
                claim_token=claim_token,
                permit=None,
                target_state=EffectState.NEEDS_REVIEW,
                observed_version=None,
                external_ref=lookup.external_ref,
                error_detail=lookup.detail or "Partial effect reported during reconciliation",
                now=now,
            )

        if lookup.status == AdapterLookupStatus.FAILED:
            return await self._gateway._finalize_non_confirmed(
                tenant_id=snap.tenant_id,
                receipt_id=snap.id,
                claim_token=claim_token,
                permit=None,
                target_state=EffectState.FAILED,
                observed_version=None,
                external_ref=lookup.external_ref,
                error_detail=lookup.detail or "Failed effect reported during reconciliation",
                now=now,
            )

        # LookupStatus.NOT_FOUND within retention horizon:
        # Legacy pre-Stage-3 receipts (missing adapter_digest or action_payload) must NEVER
        # be marked FAILED or blindly re-applied when NOT_FOUND (e.g. after simulator restart).
        is_legacy_receipt = (
            not snap.adapter_digest or not snap.action_digest or snap.action_payload is None
        )
        if is_legacy_receipt:
            return await self._gateway._finalize_non_confirmed(
                tenant_id=snap.tenant_id,
                receipt_id=snap.id,
                claim_token=claim_token,
                permit=None,
                target_state=EffectState.NEEDS_REVIEW,
                observed_version=None,
                external_ref=None,
                error_detail=(
                    "Effect not found at target for legacy unverified receipt; "
                    "human review required"
                ),
                now=now,
            )

        # Check if we can safely perform a controlled retry or must mark FAILED
        if (
            allow_controlled_retry
            and snap.attempt_count < self._max_apply_attempts
            and snap.action_payload is not None
            and bool(snap.action_digest)
            and bool(snap.adapter_digest)
        ):
            live_snap = await adapter.read_resource(snap.tenant_id, snap.target_id)
            if live_snap.current_version == snap.expected_version:
                can_retry = await self._try_mark_controlled_retry(
                    snap=snap, claim_token=claim_token, now=now
                )
                if can_retry:
                    apply_req = AdapterApplyRequest(
                        tenant_id=snap.tenant_id,
                        target_id=snap.target_id,
                        effect_id=snap.idempotency_key,
                        fence=snap.fence,
                        expected_version=snap.expected_version,
                        action_kind=str(snap.action_payload.get("action_kind", "restart_service")),
                        action_digest=snap.action_digest,
                        parameters=dict(snap.action_payload.get("parameters", {})),
                        correlation_id=correlation_id,
                    )
                    try:
                        retry_resp = await adapter.apply(apply_req)
                    except (AdapterTransportTimeoutError, TimeoutError, ConnectionError) as exc:
                        return await self._handle_lookup_failure(
                            snap=snap,
                            claim_token=claim_token,
                            error_detail=str(exc),
                            now=now,
                        )
                    if retry_resp.status in (
                        AdapterApplyStatus.APPLIED,
                        AdapterApplyStatus.DUPLICATE_APPLIED,
                    ):
                        confirmed = await self._gateway._transition_to_confirmed(
                            tenant_id=snap.tenant_id,
                            receipt_id=snap.id,
                            claim_token=claim_token,
                            observed_version=retry_resp.observed_version,
                            external_ref=retry_resp.external_ref,
                            now=now,
                        )
                        return await self._gateway.verify_and_finalize_confirmed_receipt(
                            receipt_id=confirmed.id,
                            tenant_id=snap.tenant_id,
                            permit=None,
                            adapter=adapter,
                            claim_token=claim_token,
                            correlation_id=correlation_id,
                            now=now,
                        )
                    if retry_resp.status == AdapterApplyStatus.PARTIAL_FAILURE:
                        return await self._gateway._finalize_non_confirmed(
                            tenant_id=snap.tenant_id,
                            receipt_id=snap.id,
                            claim_token=claim_token,
                            permit=None,
                            target_state=EffectState.NEEDS_REVIEW,
                            observed_version=retry_resp.observed_version,
                            external_ref=retry_resp.external_ref,
                            error_detail=retry_resp.detail
                            or "Partial failure during controlled retry",
                            now=now,
                        )

        return await self._gateway._finalize_non_confirmed(
            tenant_id=snap.tenant_id,
            receipt_id=snap.id,
            claim_token=claim_token,
            permit=None,
            target_state=EffectState.FAILED,
            observed_version=None,
            external_ref=None,
            error_detail="Effect not found at target and retry budget/preconditions exhausted",
            now=now,
        )

    async def _try_mark_controlled_retry(
        self,
        *,
        snap: EffectReceiptSnapshot,
        claim_token: uuid.UUID,
        now: datetime,
    ) -> bool:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(EffectReceipt)
                    .where(
                        EffectReceipt.id == snap.id,
                        EffectReceipt.tenant_id == snap.tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None or row.claim_token != claim_token:
                return False

            epoch = (
                await session.execute(
                    select(ResourceEpoch)
                    .where(
                        ResourceEpoch.tenant_id == snap.tenant_id,
                        ResourceEpoch.target_id == snap.target_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            res = (
                await session.execute(
                    select(ResourceReservation)
                    .where(
                        ResourceReservation.tenant_id == snap.tenant_id,
                        ResourceReservation.target_id == snap.target_id,
                        ResourceReservation.fence == snap.fence,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()

            if (
                epoch is None
                or res is None
                or epoch.latest_fence != snap.fence
                or res.state != ReservationState.DISPATCHED.value
                or res.expires_at <= now
            ):
                return False

            validate_effect_transition(
                EffectState(row.state), EffectState.SENT, allow_controlled_retry=True
            )
            row.state = EffectState.SENT.value
            row.attempt_count += 1
            row.updated_at = now
            await session.commit()
            return True

    async def _handle_lookup_failure(
        self,
        *,
        snap: EffectReceiptSnapshot,
        claim_token: uuid.UUID,
        error_detail: str,
        now: datetime,
    ) -> EffectReceiptSnapshot:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(EffectReceipt)
                    .where(
                        EffectReceipt.id == snap.id,
                        EffectReceipt.tenant_id == snap.tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None or row.claim_token != claim_token:
                raise StaleEffectClaimError(snap.id)

            row.attempt_count += 1
            row.last_error = error_detail
            row.updated_at = now

            if row.attempt_count > self._max_reconcile_attempts:
                validate_effect_transition(EffectState(row.state), EffectState.NEEDS_REVIEW)
                row.state = EffectState.NEEDS_REVIEW.value
                row.lease_expires_at = None
                await self._gateway._release_reservation_for_receipt(
                    session, row, permit=None, observed_version=None
                )
                await session.commit()
                metrics.inc("effect_needs_review_total")
                return _to_receipt_snapshot(row)

            row.state = EffectState.UNKNOWN.value
            row.lease_expires_at = now + timedelta(seconds=5)
            await session.commit()
            return _to_receipt_snapshot(row)
