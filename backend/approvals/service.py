"""Feature P2 — Version-Bound Approval Service with Fail-Closed Invariants."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.domain.approval import (
    ApprovalCreateRequest,
    ApprovalDecisionRequest,
    ApprovalDigestMismatchError,
    ApprovalExpiredError,
    ApprovalNotFoundError,
    ApprovalPolicyReleaseForbiddenError,
    ApprovalResponse,
    ApprovalRevokedError,
    ApprovalRevokeRequest,
    ApprovalState,
    ApprovalVersionMismatchError,
    InvalidApprovalTransitionError,
    StaleContextError,
    validate_approval_transition,
    validate_reviewer_role,
)
from backend.domain.incident import (
    ApprovedExecutionSignalInput,
    IncidentNotFoundError,
    make_incident_workflow_id,
)
from backend.effects.fencing import ApprovedPlanContext
from backend.observability.metrics import emit_event, metrics
from backend.persistence.models import ApprovalAudit, ApprovalRequest, Incident


class TemporalApprovalSignalerProtocol(Protocol):
    """Protocol for signaling IncidentWorkflow upon human approval decision."""

    async def signal_approve_and_execute(
        self, workflow_id: str, execution_input: ApprovedExecutionSignalInput
    ) -> None: ...

    async def signal_resolve_awaiting_approval(
        self, workflow_id: str, target_status: str
    ) -> None: ...


def _to_response(row: ApprovalRequest) -> ApprovalResponse:
    return ApprovalResponse(
        id=row.id,
        tenant_id=row.tenant_id,
        incident_id=row.incident_id,
        step_id=row.step_id,
        plan_id=row.plan_id,
        canonical_plan_digest=row.canonical_plan_digest,
        action_digest=row.action_digest,
        action_payload=row.action_payload if isinstance(row.action_payload, dict) else {},
        target_id=row.target_id,
        expected_resource_version=row.expected_resource_version,
        policy_version=row.policy_version,
        state=ApprovalState(row.state),
        reviewer_id=row.reviewer_id,
        reviewer_role=row.reviewer_role,
        expires_at=row.expires_at,
        decision_at=row.decision_at,
        reason=row.reason,
        dispatch_lease_id=row.dispatch_lease_id,
        dispatch_lease_expires_at=row.dispatch_lease_expires_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class ApprovalService:
    """Authoritative P2 Approval Service managing durable approval lifecycle."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        workflow_signaler: TemporalApprovalSignalerProtocol | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._signaler = workflow_signaler

    async def create_approval_request(
        self,
        tenant_id: uuid.UUID,
        incident_id: uuid.UUID,
        req: ApprovalCreateRequest,
        *,
        now: datetime | None = None,
    ) -> ApprovalResponse:
        current_time = now or datetime.now(UTC)
        expires_at = current_time + timedelta(seconds=req.ttl_seconds)

        async with self._session_factory() as session:
            # 1. Verify incident exists under tenant
            inc_row = (
                await session.execute(
                    select(Incident).where(
                        Incident.tenant_id == tenant_id,
                        Incident.id == incident_id,
                    )
                )
            ).scalar_one_or_none()
            if inc_row is None:
                raise IncidentNotFoundError(incident_id)

            # 2. Check for existing active approval on this incident (idempotency / singleton active)
            active_row = (
                await session.execute(
                    select(ApprovalRequest).where(
                        ApprovalRequest.tenant_id == tenant_id,
                        ApprovalRequest.incident_id == incident_id,
                        ApprovalRequest.state.in_(
                            [
                                ApprovalState.PENDING.value,
                                ApprovalState.APPROVED.value,
                                ApprovalState.DISPATCHING.value,
                            ]
                        ),
                    )
                )
            ).scalar_one_or_none()

            if active_row is not None:
                # If identical pending request, return existing
                if (
                    active_row.state == ApprovalState.PENDING.value
                    and active_row.canonical_plan_digest == req.canonical_plan_digest
                    and active_row.action_digest == req.action_digest
                    and active_row.expected_resource_version == req.expected_resource_version
                    and active_row.expires_at > current_time
                ):
                    return _to_response(active_row)

                # If existing approval expired, advance it
                if active_row.expires_at <= current_time:
                    active_row.state = ApprovalState.EXPIRED.value
                    active_row.updated_at = current_time
                    session.add(
                        ApprovalAudit(
                            id=uuid.uuid4(),
                            tenant_id=tenant_id,
                            approval_id=active_row.id,
                            action=ApprovalState.EXPIRED.value,
                            actor_id="system",
                            actor_role="system",
                            reason="Lease expired prior to new approval request",
                            timestamp=current_time,
                        )
                    )
                    await session.flush()
                else:
                    # Active non-expired approval already present
                    return _to_response(active_row)

            # 3. Create fresh approval request
            approval_id = uuid.uuid4()
            approval = ApprovalRequest(
                id=approval_id,
                tenant_id=tenant_id,
                incident_id=incident_id,
                step_id=req.step_id,
                plan_id=req.plan_id,
                canonical_plan_digest=req.canonical_plan_digest,
                action_digest=req.action_digest,
                action_payload=req.action_payload,
                target_id=req.target_id,
                expected_resource_version=req.expected_resource_version,
                policy_version=req.policy_version,
                state=ApprovalState.PENDING.value,
                expires_at=expires_at,
                created_at=current_time,
                updated_at=current_time,
            )
            session.add(approval)
            await session.flush()
            session.add(
                ApprovalAudit(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    approval_id=approval_id,
                    action="CREATED",
                    actor_id="investigator",
                    actor_role="investigator",
                    reason="Plan proposed by adaptive investigator",
                    timestamp=current_time,
                    metadata_json={
                        "plan_id": req.plan_id,
                        "canonical_plan_digest": req.canonical_plan_digest,
                    },
                )
            )
            await session.commit()
            emit_event(
                "approval.created",
                tenant_id=str(tenant_id),
                incident_id=str(incident_id),
                approval_id=str(approval_id),
            )
            metrics.inc("approval_created_total")
            return _to_response(approval)

    async def get_approval(
        self,
        tenant_id: uuid.UUID,
        approval_id: uuid.UUID,
        *,
        now: datetime | None = None,
    ) -> ApprovalResponse:
        current_time = now or datetime.now(UTC)
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(ApprovalRequest).where(
                        ApprovalRequest.tenant_id == tenant_id,
                        ApprovalRequest.id == approval_id,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                raise ApprovalNotFoundError(approval_id)

            # Check lazy expiry
            if (
                row.state
                in (
                    ApprovalState.PENDING.value,
                    ApprovalState.APPROVED.value,
                    ApprovalState.DISPATCHING.value,
                )
                and row.expires_at <= current_time
            ):
                row.state = ApprovalState.EXPIRED.value
                row.updated_at = current_time
                session.add(
                    ApprovalAudit(
                        id=uuid.uuid4(),
                        tenant_id=tenant_id,
                        approval_id=approval_id,
                        action=ApprovalState.EXPIRED.value,
                        actor_id="system",
                        actor_role="system",
                        reason="Approval expired",
                        timestamp=current_time,
                    )
                )
                await session.commit()
                emit_event("approval.expired", approval_id=str(approval_id))

            return _to_response(row)

    async def approve(
        self,
        tenant_id: uuid.UUID,
        approval_id: uuid.UUID,
        decision: ApprovalDecisionRequest,
        *,
        now: datetime | None = None,
    ) -> ApprovalResponse:
        current_time = now or datetime.now(UTC)
        validated_role = validate_reviewer_role(decision.reviewer_role)

        async with self._session_factory() as session:
            # 1. Fetch approval with row lock
            row = (
                await session.execute(
                    select(ApprovalRequest)
                    .where(
                        ApprovalRequest.tenant_id == tenant_id,
                        ApprovalRequest.id == approval_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                raise ApprovalNotFoundError(approval_id)

            # 2. Check expiry
            if row.expires_at <= current_time:
                row.state = ApprovalState.EXPIRED.value
                row.updated_at = current_time
                session.add(
                    ApprovalAudit(
                        id=uuid.uuid4(),
                        tenant_id=tenant_id,
                        approval_id=approval_id,
                        action="EXPIRED",
                        actor_id=decision.reviewer_id,
                        actor_role=decision.reviewer_role,
                        reason=f"Approval expired at {row.expires_at.isoformat()}",
                    )
                )
                await session.commit()
                raise ApprovalExpiredError(approval_id)

            # 3. Idempotent check
            if row.state == ApprovalState.APPROVED.value:
                if row.reviewer_id == decision.reviewer_id:
                    return _to_response(row)
                raise InvalidApprovalTransitionError(
                    ApprovalState.APPROVED, ApprovalState.APPROVED
                )

            # 4. Validate transition from PENDING
            validate_approval_transition(ApprovalState(row.state), ApprovalState.APPROVED)

            # Stage 11.1 Gate F: Verify policy release control permits plan approval
            from backend.releases.router_service import PolicyRouterService
            router_svc = PolicyRouterService(self._session_factory)
            allowed, reason = await router_svc.verify_consequential_mutation_allowed(
                tenant_id=tenant_id,
                incident_id=row.incident_id,
            )
            if not allowed:
                raise ApprovalPolicyReleaseForbiddenError(
                    reason or "Plan approval blocked by policy release control"
                )

            # 5. Check incident status version optimistic concurrency guard if requested
            inc_row = (
                await session.execute(
                    select(Incident).where(
                        Incident.tenant_id == tenant_id,
                        Incident.id == row.incident_id,
                    )
                )
            ).scalar_one_or_none()
            if inc_row is None:
                raise IncidentNotFoundError(row.incident_id)

            if (
                decision.expected_incident_status_version is not None
                and inc_row.status_version != decision.expected_incident_status_version
            ):
                raise StaleContextError(
                    expected_version=decision.expected_incident_status_version,
                    current_version=inc_row.status_version,
                )

            # 6. Apply approval
            row.state = ApprovalState.APPROVED.value
            row.reviewer_id = decision.reviewer_id
            row.reviewer_role = validated_role
            row.decision_at = current_time
            row.reason = decision.reason
            row.updated_at = current_time

            session.add(
                ApprovalAudit(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    approval_id=approval_id,
                    action="APPROVED",
                    actor_id=decision.reviewer_id,
                    actor_role=validated_role,
                    reason=decision.reason,
                    timestamp=current_time,
                )
            )
            await session.commit()
            emit_event(
                "approval.approved",
                approval_id=str(approval_id),
                reviewer_id=decision.reviewer_id,
                reviewer_role=validated_role,
            )
            metrics.inc("approval_approved_total")

            # 7. Signal Temporal IncidentWorkflow if signaler configured
            if self._signaler is not None:
                sig_input = ApprovedExecutionSignalInput(
                    approval_id=str(row.id),
                    tenant_id=str(row.tenant_id),
                    incident_id=str(row.incident_id),
                    target_id=row.target_id,
                    adapter_id=row.target_id,
                    plan_digest=row.canonical_plan_digest,
                    action_kind=row.action_payload.get("action_kind", "restart_pod")
                    if isinstance(row.action_payload, dict)
                    else "restart_pod",
                    action_digest=row.action_digest,
                    expected_version=row.expected_resource_version,
                    parameters=row.action_payload.get("parameters", {})
                    if isinstance(row.action_payload, dict)
                    else {},
                    approved_by_role=validated_role,
                    execution_mode="SIMULATOR_APPROVED",
                    expires_at_iso=row.expires_at.isoformat(),
                )
                wf_id = make_incident_workflow_id(row.tenant_id, row.incident_id)
                await self._signaler.signal_approve_and_execute(wf_id, sig_input)

            return _to_response(row)

    async def reject(
        self,
        tenant_id: uuid.UUID,
        approval_id: uuid.UUID,
        decision: ApprovalDecisionRequest,
        *,
        now: datetime | None = None,
    ) -> ApprovalResponse:
        current_time = now or datetime.now(UTC)
        validated_role = validate_reviewer_role(decision.reviewer_role)

        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(ApprovalRequest)
                    .where(
                        ApprovalRequest.tenant_id == tenant_id,
                        ApprovalRequest.id == approval_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                raise ApprovalNotFoundError(approval_id)

            validate_approval_transition(ApprovalState(row.state), ApprovalState.REJECTED)

            row.state = ApprovalState.REJECTED.value
            row.reviewer_id = decision.reviewer_id
            row.reviewer_role = validated_role
            row.decision_at = current_time
            row.reason = decision.reason
            row.updated_at = current_time

            session.add(
                ApprovalAudit(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    approval_id=approval_id,
                    action="REJECTED",
                    actor_id=decision.reviewer_id,
                    actor_role=validated_role,
                    reason=decision.reason,
                    timestamp=current_time,
                )
            )
            await session.commit()
            emit_event("approval.rejected", approval_id=str(approval_id))
            metrics.inc("approval_rejected_total")

            if self._signaler is not None:
                wf_id = make_incident_workflow_id(row.tenant_id, row.incident_id)
                await self._signaler.signal_resolve_awaiting_approval(wf_id, "NEEDS_REVIEW")

            return _to_response(row)

    async def revoke(
        self,
        tenant_id: uuid.UUID,
        approval_id: uuid.UUID,
        revoke_req: ApprovalRevokeRequest,
        *,
        now: datetime | None = None,
    ) -> ApprovalResponse:
        current_time = now or datetime.now(UTC)
        validated_role = validate_reviewer_role(revoke_req.reviewer_role)

        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(ApprovalRequest)
                    .where(
                        ApprovalRequest.tenant_id == tenant_id,
                        ApprovalRequest.id == approval_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                raise ApprovalNotFoundError(approval_id)

            validate_approval_transition(ApprovalState(row.state), ApprovalState.REVOKED)

            row.state = ApprovalState.REVOKED.value
            row.reason = f"Revoked by {revoke_req.reviewer_id}: {revoke_req.reason}"
            row.updated_at = current_time

            session.add(
                ApprovalAudit(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    approval_id=approval_id,
                    action="REVOKED",
                    actor_id=revoke_req.reviewer_id,
                    actor_role=validated_role,
                    reason=revoke_req.reason,
                    timestamp=current_time,
                )
            )
            await session.commit()
            emit_event("approval.revoked", approval_id=str(approval_id))
            metrics.inc("approval_revoked_total")

            if self._signaler is not None:
                wf_id = make_incident_workflow_id(row.tenant_id, row.incident_id)
                await self._signaler.signal_resolve_awaiting_approval(wf_id, "NEEDS_REVIEW")

            return _to_response(row)

    async def claim_dispatch_lease(
        self,
        tenant_id: uuid.UUID,
        approval_id: uuid.UUID,
        *,
        target_version: str,
        plan_digest: str,
        action_digest: str,
        lease_seconds: float = 60.0,
        now: datetime | None = None,
    ) -> tuple[uuid.UUID, ApprovedPlanContext]:
        """Short Tx 1: Validate approval validity and lock dispatch lease before external B3 mutation.

        Guarantees:
        1. Never holds database locks across external network I/O.
        2. Revalidation enforces plan digest, action digest, target version, and expiry.
        3. Allows idempotent re-entry if the same lease is already active.
        """
        current_time = now or datetime.now(UTC)
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(ApprovalRequest)
                    .where(
                        ApprovalRequest.tenant_id == tenant_id,
                        ApprovalRequest.id == approval_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                raise ApprovalNotFoundError(approval_id)

            # Check expiry
            if row.expires_at <= current_time:
                row.state = ApprovalState.EXPIRED.value
                row.updated_at = current_time
                session.add(
                    ApprovalAudit(
                        id=uuid.uuid4(),
                        tenant_id=row.tenant_id,
                        approval_id=row.id,
                        action="EXPIRED",
                        actor_id="system",
                        actor_role="system",
                        reason=f"Approval expired at {row.expires_at.isoformat()}",
                        timestamp=current_time,
                    )
                )
                await session.commit()
                raise ApprovalExpiredError(approval_id)

            # Check state
            if row.state == ApprovalState.REVOKED.value:
                raise ApprovalRevokedError(approval_id)
            if row.state == ApprovalState.CONSUMED.value:
                raise InvalidApprovalTransitionError(
                    ApprovalState.CONSUMED, ApprovalState.DISPATCHING
                )

            # Idempotent re-entry for active lease
            if (
                row.state == ApprovalState.DISPATCHING.value
                and row.dispatch_lease_id is not None
                and row.dispatch_lease_expires_at is not None
                and row.dispatch_lease_expires_at > current_time
            ):
                    ctx = ApprovedPlanContext(
                        approval_id=row.id,
                        tenant_id=row.tenant_id,
                        incident_id=row.incident_id,
                        target_id=row.target_id,
                        plan_digest=row.canonical_plan_digest,
                        action_digest=row.action_digest,
                        expected_version=row.expected_resource_version,
                        approved_by_role=row.reviewer_role or "incident_commander",
                        execution_mode="SIMULATOR_APPROVED",
                        approved_at=row.decision_at,
                        expires_at=row.expires_at,
                    )
                    return row.dispatch_lease_id, ctx

            # If lease has expired while DISPATCHING, permit new lease claim
            if (
                row.state == ApprovalState.DISPATCHING.value
                and row.dispatch_lease_expires_at is not None
                and row.dispatch_lease_expires_at <= current_time
            ):
                pass
            elif row.state != ApprovalState.APPROVED.value:
                raise InvalidApprovalTransitionError(
                    ApprovalState(row.state), ApprovalState.DISPATCHING
                )

            # Guard plan digest, action digest, and target version
            if row.canonical_plan_digest != plan_digest:
                raise ApprovalDigestMismatchError(row.canonical_plan_digest, plan_digest)
            if row.action_digest != action_digest:
                raise ApprovalDigestMismatchError(row.action_digest, action_digest)

            # Stage 11.1 Gate F: Verify policy release control permits consequential dispatch
            from backend.releases.router_service import PolicyRouterService
            router_svc = PolicyRouterService(self._session_factory)
            allowed, reason = await router_svc.verify_consequential_mutation_allowed(
                tenant_id=tenant_id,
                incident_id=row.incident_id,
            )
            if not allowed:
                raise ApprovalPolicyReleaseForbiddenError(
                    reason or "Consequential dispatch blocked by policy release control"
                )

            if row.expected_resource_version != target_version:
                row.state = ApprovalState.INVALIDATED.value
                row.updated_at = current_time
                session.add(
                    ApprovalAudit(
                        id=uuid.uuid4(),
                        tenant_id=row.tenant_id,
                        approval_id=row.id,
                        action="INVALIDATED",
                        actor_id="system",
                        actor_role="system",
                        reason=f"Target resource version drift: expected {row.expected_resource_version}, observed {target_version}",
                        timestamp=current_time,
                    )
                )
                await session.commit()
                raise ApprovalVersionMismatchError(row.expected_resource_version, target_version)

            lease_id = uuid.uuid4()
            row.state = ApprovalState.DISPATCHING.value
            row.dispatch_lease_id = lease_id
            row.dispatch_lease_expires_at = current_time + timedelta(seconds=lease_seconds)
            row.updated_at = current_time

            session.add(
                ApprovalAudit(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    approval_id=approval_id,
                    action="DISPATCHED",
                    actor_id="system",
                    actor_role="system",
                    reason=f"Claimed dispatch lease {lease_id}",
                    timestamp=current_time,
                )
            )
            await session.commit()
            emit_event(
                "approval.dispatch_leased",
                approval_id=str(approval_id),
                lease_id=str(lease_id),
            )

            ctx = ApprovedPlanContext(
                approval_id=row.id,
                tenant_id=row.tenant_id,
                incident_id=row.incident_id,
                target_id=row.target_id,
                plan_digest=row.canonical_plan_digest,
                action_digest=row.action_digest,
                expected_version=row.expected_resource_version,
                approved_by_role=row.reviewer_role or "incident_commander",
                execution_mode="SIMULATOR_APPROVED",
                approved_at=row.decision_at,
                expires_at=row.expires_at,
            )
            return lease_id, ctx

    async def finalize_consumed(
        self,
        tenant_id: uuid.UUID,
        approval_id: uuid.UUID,
        lease_id: uuid.UUID,
        *,
        now: datetime | None = None,
    ) -> None:
        """Short Tx 2: Transition approval to CONSUMED after external effect verification."""
        current_time = now or datetime.now(UTC)
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(ApprovalRequest)
                    .where(
                        ApprovalRequest.tenant_id == tenant_id,
                        ApprovalRequest.id == approval_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                return

            if row.state == ApprovalState.CONSUMED.value:
                return

            if row.state == ApprovalState.DISPATCHING.value and row.dispatch_lease_id == lease_id:
                row.state = ApprovalState.CONSUMED.value
                row.updated_at = current_time
                session.add(
                    ApprovalAudit(
                        id=uuid.uuid4(),
                        tenant_id=tenant_id,
                        approval_id=approval_id,
                        action="CONSUMED",
                        actor_id="system",
                        actor_role="system",
                        reason=f"Effect verified for lease {lease_id}",
                        timestamp=current_time,
                    )
                )
                await session.commit()
                emit_event("approval.consumed", approval_id=str(approval_id))
                metrics.inc("approval_consumed_total")
