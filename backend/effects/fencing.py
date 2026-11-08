"""Feature B2 — Version-Fenced Resource Reservation, MutationPermit, and Fail-Closed Approval Boundary."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.adapters.conformance import MutationCapabilityGate
from backend.adapters.protocol import TargetAdapterProtocol
from backend.domain.incident import (
    DegradedRecoveryGate,
    DomainError,
    IncidentNotFoundError,
    IncidentStatus,
)
from backend.observability.metrics import emit_event, metrics
from backend.persistence.models import (
    EffectReceipt,
    Incident,
    ResourceEpoch,
    ResourceReservation,
)


class ReservationState(StrEnum):
    RESERVED = "RESERVED"
    DISPATCHED = "DISPATCHED"
    RELEASED = "RELEASED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


ACTIVE_RESERVATION_STATES: frozenset[ReservationState] = frozenset(
    {ReservationState.RESERVED, ReservationState.DISPATCHED}
)

TERMINAL_RESERVATION_STATES: frozenset[ReservationState] = frozenset(
    {
        ReservationState.RELEASED,
        ReservationState.EXPIRED,
        ReservationState.CANCELLED,
    }
)

ALLOWED_RESERVATION_TRANSITIONS: dict[ReservationState, frozenset[ReservationState]] = {
    ReservationState.RESERVED: frozenset(
        {
            ReservationState.DISPATCHED,
            ReservationState.EXPIRED,
            ReservationState.CANCELLED,
            ReservationState.RELEASED,
        }
    ),
    ReservationState.DISPATCHED: frozenset(
        {
            ReservationState.RELEASED,
            ReservationState.EXPIRED,
        }
    ),
    ReservationState.RELEASED: frozenset(),
    ReservationState.EXPIRED: frozenset(),
    ReservationState.CANCELLED: frozenset(),
}

FORBIDDEN_APPROVAL_ROLES: frozenset[str] = frozenset(
    {"agent", "model", "investigator", "llm", "autonomous_agent"}
)


class ReservationError(DomainError):
    """Base error for B2 reservation and fencing failures."""


class InvalidReservationStateTransitionError(ReservationError):
    def __init__(self, current: ReservationState, target: ReservationState) -> None:
        self.current = current
        self.target = target
        super().__init__(f"Illegal reservation state transition from {current} to {target}")


class ApprovalRequiredError(ReservationError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Fail-closed approval rejection: {reason}")


class ResourceAlreadyReservedError(ReservationError):
    def __init__(self, tenant_id: uuid.UUID, target_id: str, holder_incident_id: uuid.UUID) -> None:
        self.tenant_id = tenant_id
        self.target_id = target_id
        self.holder_incident_id = holder_incident_id
        super().__init__(
            f"Target '{target_id}' is already reserved by incident {holder_incident_id}"
        )


class ResourceUncertainEffectPendingError(ResourceAlreadyReservedError):
    """Raised when a target has an unresolved SENT/UNKNOWN/CONFIRMED receipt in B3."""


class StaleFenceError(ReservationError):
    def __init__(self, target_id: str, fence: int, latest_fence: int, reason: str = "") -> None:
        self.target_id = target_id
        self.fence = fence
        self.latest_fence = latest_fence
        msg = (
            f"Stale or invalid fence {fence} (latest_fence={latest_fence}) on target '{target_id}'"
        )
        if reason:
            msg = f"{msg}: {reason}"
        super().__init__(msg)


class ReservationExpiredError(StaleFenceError):
    """Raised when a reservation lease has expired."""


class TargetVersionConflictError(ReservationError):
    def __init__(self, target_id: str, expected_version: str, observed_version: str) -> None:
        self.target_id = target_id
        self.expected_version = expected_version
        self.observed_version = observed_version
        super().__init__(
            f"Target '{target_id}' version conflict: expected '{expected_version}', "
            f"observed '{observed_version}'"
        )


def validate_reservation_transition(current: ReservationState, target: ReservationState) -> bool:
    if current == target:
        return False
    allowed = ALLOWED_RESERVATION_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise InvalidReservationStateTransitionError(current, target)
    return True


@dataclass(frozen=True)
class ApprovedPlanContext:
    """Fail-closed P2 approval contract for Stage 3 simulator execution."""

    approval_id: uuid.UUID
    tenant_id: uuid.UUID
    incident_id: uuid.UUID
    target_id: str
    plan_digest: str
    action_digest: str
    expected_version: str
    approved_by_role: str = "incident_commander"
    execution_mode: str = "SIMULATOR_APPROVED"
    approved_at: datetime | None = None
    expires_at: datetime | None = None


@dataclass(frozen=True)
class MutationPermit:
    """Server-minted B2 mutation permit binding reservation, fence, version, and adapter digest."""

    reservation_id: uuid.UUID
    tenant_id: uuid.UUID
    incident_id: uuid.UUID
    target_id: str
    adapter_id: str
    adapter_version: str
    adapter_code_digest: str
    plan_digest: str
    action_digest: str
    fence: int
    expected_version: str
    expires_at: datetime
    issued_at: datetime


def verify_approved_plan_context(
    approval: ApprovedPlanContext | None,
    *,
    tenant_id: uuid.UUID,
    incident_id: uuid.UUID,
    target_id: str,
    plan_digest: str,
    action_digest: str,
    expected_version: str,
    is_simulator_adapter: bool,
    now: datetime | None = None,
) -> ApprovedPlanContext:
    """Deterministic fail-closed validator for the future P2 approval dependency."""
    if approval is None:
        raise ApprovalRequiredError("Missing ApprovedPlanContext (P2 approval required)")

    check_time = now or datetime.now(UTC)
    if approval.execution_mode != "SIMULATOR_APPROVED":
        raise ApprovalRequiredError(
            f"Unsupported execution_mode '{approval.execution_mode}'; only SIMULATOR_APPROVED is permitted in Stage 3"
        )
    if not is_simulator_adapter:
        raise ApprovalRequiredError("Non-simulator external mutation is denied in Stage 3")

    role_norm = approval.approved_by_role.strip().lower()
    if not role_norm or role_norm in FORBIDDEN_APPROVAL_ROLES:
        raise ApprovalRequiredError(
            f"Caller role '{approval.approved_by_role}' is forbidden from approving plans"
        )

    if approval.tenant_id != tenant_id:
        raise ApprovalRequiredError("Approval tenant_id mismatch")
    if approval.incident_id != incident_id:
        raise ApprovalRequiredError("Approval incident_id mismatch")
    if approval.target_id != target_id:
        raise ApprovalRequiredError("Approval target_id mismatch")
    if not approval.plan_digest or approval.plan_digest != plan_digest:
        raise ApprovalRequiredError("Approval plan_digest mismatch")
    if not approval.action_digest or approval.action_digest != action_digest:
        raise ApprovalRequiredError("Approval action_digest mismatch")
    if not approval.expected_version or approval.expected_version != expected_version:
        raise ApprovalRequiredError("Approval expected_version mismatch")
    if approval.expires_at is None or approval.expires_at <= check_time:
        raise ApprovalRequiredError("Approval context has expired")

    return approval


class ResourceReservationManager:
    """Tenant-scoped B2 version-fenced resource reservation manager."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._capability_gate = MutationCapabilityGate(session_factory)

    async def acquire_reservation(
        self,
        *,
        approval: ApprovedPlanContext | None,
        adapter: TargetAdapterProtocol,
        lease_seconds: float = 30.0,
        correlation_id: str = "",
        now: datetime | None = None,
    ) -> MutationPermit:
        """Acquire a version-fenced resource reservation in a short PostgreSQL transaction.

        1. Validates `ApprovedPlanContext` (fail-closed P2 boundary).
        2. Verifies E8 adapter conformance, version, and SHA-256 `code_digest`.
        3. Reads live target state via `adapter.read_resource()` outside the DB transaction.
        4. Locks `resource_epoch(tenant_id, target_id)` with `FOR UPDATE`, expires stale
           `RESERVED` leases, enforces at-most-one active reservation, verifies target
           version CAS, increments `latest_fence = latest_fence + 1`, and inserts
           `resource_reservation(state='RESERVED', fence=n)`.
        """
        if approval is None:
            raise ApprovalRequiredError("Missing ApprovedPlanContext")

        DegradedRecoveryGate.check_mutation_allowed()

        current_time = now or datetime.now(UTC)
        prof = adapter.profile()
        verify_approved_plan_context(
            approval,
            tenant_id=approval.tenant_id,
            incident_id=approval.incident_id,
            target_id=approval.target_id,
            plan_digest=approval.plan_digest,
            action_digest=approval.action_digest,
            expected_version=approval.expected_version,
            is_simulator_adapter=prof.is_simulator,
            now=current_time,
        )

        # Verify E8 adapter conformance before touching target or DB reservation
        conformance = await self._capability_gate.verify_mutation_allowed(
            adapter, correlation_id=correlation_id, now=current_time
        )

        # Read live target resource version outside the DB transaction
        live_snapshot = await adapter.read_resource(approval.tenant_id, approval.target_id)

        async with self._session_factory() as session:
            # Verify incident belongs to tenant and is in an eligible state
            inc = (
                await session.execute(
                    select(Incident).where(
                        Incident.tenant_id == approval.tenant_id,
                        Incident.id == approval.incident_id,
                    )
                )
            ).scalar_one_or_none()
            if inc is None:
                raise IncidentNotFoundError(approval.incident_id)
            inc_status = IncidentStatus(inc.status_projection)
            if inc_status not in (
                IncidentStatus.AWAITING_APPROVAL,
                IncidentStatus.EXECUTING,
            ):
                raise ApprovalRequiredError(
                    f"Incident {approval.incident_id} is in state {inc_status}, "
                    "expected AWAITING_APPROVAL or EXECUTING"
                )

            # Upsert resource_epoch row safely under concurrency, then lock with FOR UPDATE
            upsert_stmt = (
                pg_insert(ResourceEpoch)
                .values(
                    tenant_id=approval.tenant_id,
                    target_id=approval.target_id,
                    latest_fence=0,
                    version_seen=None,
                )
                .on_conflict_do_nothing(index_elements=["tenant_id", "target_id"])
            )
            await session.execute(upsert_stmt)

            epoch = (
                await session.execute(
                    select(ResourceEpoch)
                    .where(
                        ResourceEpoch.tenant_id == approval.tenant_id,
                        ResourceEpoch.target_id == approval.target_id,
                    )
                    .with_for_update()
                )
            ).scalar_one()

            # Sync epoch fence watermark with live target and any existing reservation/receipt fences
            max_res_fence = (
                await session.execute(
                    select(func.coalesce(func.max(ResourceReservation.fence), 0)).where(
                        ResourceReservation.tenant_id == approval.tenant_id,
                        ResourceReservation.target_id == approval.target_id,
                    )
                )
            ).scalar_one()
            max_rec_fence = (
                await session.execute(
                    select(func.coalesce(func.max(EffectReceipt.fence), 0)).where(
                        EffectReceipt.tenant_id == approval.tenant_id,
                        EffectReceipt.target_id == approval.target_id,
                    )
                )
            ).scalar_one()
            epoch.latest_fence = max(
                epoch.latest_fence,
                live_snapshot.highest_fence_seen,
                int(max_res_fence),
                int(max_rec_fence),
            )

            # Check target version CAS against both live target and epoch.version_seen
            if live_snapshot.current_version != approval.expected_version or (
                epoch.version_seen is not None
                and epoch.version_seen != approval.expected_version
                and epoch.version_seen == live_snapshot.current_version
            ):
                epoch.version_seen = live_snapshot.current_version
                await session.commit()
                metrics.inc("target_version_conflicts_total")
                emit_event(
                    "target.version_conflict",
                    incident_id=approval.incident_id,
                    correlation_id=correlation_id,
                    target_id=approval.target_id,
                    expected_version=approval.expected_version,
                    observed_version=live_snapshot.current_version,
                    level="WARNING",
                )
                raise TargetVersionConflictError(
                    approval.target_id,
                    approval.expected_version,
                    live_snapshot.current_version,
                )

            epoch.version_seen = live_snapshot.current_version

            # Inspect any active reservation for (tenant_id, target_id)
            active_rows = (
                (
                    await session.execute(
                        select(ResourceReservation)
                        .where(
                            ResourceReservation.tenant_id == approval.tenant_id,
                            ResourceReservation.target_id == approval.target_id,
                            ResourceReservation.state.in_(
                                [ReservationState.RESERVED.value, ReservationState.DISPATCHED.value]
                            ),
                        )
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )

            for active in active_rows:
                if active.expires_at <= current_time:
                    if active.state == ReservationState.DISPATCHED.value:
                        # Check if an unresolved B3 receipt exists for this dispatched fence
                        unresolved = (
                            await session.execute(
                                select(EffectReceipt.id).where(
                                    EffectReceipt.tenant_id == approval.tenant_id,
                                    EffectReceipt.target_id == approval.target_id,
                                    EffectReceipt.fence == active.fence,
                                    EffectReceipt.state.in_(["SENT", "UNKNOWN", "CONFIRMED"]),
                                )
                            )
                        ).scalar_one_or_none()
                        if unresolved is not None:
                            metrics.inc("reservation_conflicts_total")
                            emit_event(
                                "reservation.conflict",
                                incident_id=approval.incident_id,
                                correlation_id=correlation_id,
                                target_id=approval.target_id,
                                holder_incident_id=str(active.holder_incident_id),
                                reason="unresolved_dispatched_effect",
                                level="WARNING",
                            )
                            raise ResourceUncertainEffectPendingError(
                                approval.tenant_id,
                                approval.target_id,
                                active.holder_incident_id,
                            )

                    active.state = ReservationState.EXPIRED.value
                    metrics.inc("lease_expired_total")
                    emit_event(
                        "lease.expired",
                        incident_id=active.holder_incident_id,
                        correlation_id=correlation_id,
                        target_id=approval.target_id,
                        expired_fence=active.fence,
                        level="WARNING",
                    )
                else:
                    # Active non-expired reservation exists
                    if (
                        active.holder_incident_id == approval.incident_id
                        and active.action_digest == approval.action_digest
                        and active.state == ReservationState.RESERVED.value
                    ):
                        # Idempotent return of existing active reservation for the same incident+action
                        return MutationPermit(
                            reservation_id=active.id,
                            tenant_id=active.tenant_id,
                            incident_id=active.holder_incident_id,
                            target_id=active.target_id,
                            adapter_id=prof.adapter_id,
                            adapter_version=prof.version,
                            adapter_code_digest=conformance.code_digest,
                            plan_digest=approval.plan_digest,
                            action_digest=active.action_digest,
                            fence=active.fence,
                            expected_version=approval.expected_version,
                            expires_at=active.expires_at,
                            issued_at=active.created_at,
                        )

                    metrics.inc("reservation_conflicts_total")
                    emit_event(
                        "reservation.conflict",
                        incident_id=approval.incident_id,
                        correlation_id=correlation_id,
                        target_id=approval.target_id,
                        holder_incident_id=str(active.holder_incident_id),
                        active_fence=active.fence,
                        level="WARNING",
                    )
                    raise ResourceAlreadyReservedError(
                        approval.tenant_id,
                        approval.target_id,
                        active.holder_incident_id,
                    )

            # Flush any EXPIRED state updates before inserting the new active reservation
            await session.flush()

            new_fence = epoch.latest_fence + 1
            epoch.latest_fence = new_fence
            expires_at = current_time + timedelta(seconds=lease_seconds)
            reservation_id = uuid.uuid4()

            new_res = ResourceReservation(
                id=reservation_id,
                tenant_id=approval.tenant_id,
                target_id=approval.target_id,
                holder_incident_id=approval.incident_id,
                action_digest=approval.action_digest,
                fence=new_fence,
                expires_at=expires_at,
                state=ReservationState.RESERVED.value,
                created_at=current_time,
            )
            session.add(new_res)
            await session.commit()

            metrics.inc("reservation_acquired_total")
            return MutationPermit(
                reservation_id=reservation_id,
                tenant_id=approval.tenant_id,
                incident_id=approval.incident_id,
                target_id=approval.target_id,
                adapter_id=prof.adapter_id,
                adapter_version=prof.version,
                adapter_code_digest=conformance.code_digest,
                plan_digest=approval.plan_digest,
                action_digest=approval.action_digest,
                fence=new_fence,
                expected_version=approval.expected_version,
                expires_at=expires_at,
                issued_at=current_time,
            )

    async def mark_dispatched(
        self,
        permit: MutationPermit,
        *,
        session: AsyncSession | None = None,
        extend_lease_seconds: float = 30.0,
        correlation_id: str = "",
        now: datetime | None = None,
    ) -> None:
        """Validate a MutationPermit against PostgreSQL and transition RESERVED -> DISPATCHED.

        Can run either in its own short transaction or inside an existing session (used by B3
        ActionGateway to atomically transition RESERVED -> DISPATCHED and PREPARED -> SENT).
        """
        current_time = now or datetime.now(UTC)
        if session is not None:
            await self._mark_dispatched_in_session(
                session,
                permit,
                current_time=current_time,
                extend_lease_seconds=extend_lease_seconds,
                correlation_id=correlation_id,
                commit_on_expire=False,
            )
            return

        async with self._session_factory() as own_session:
            await self._mark_dispatched_in_session(
                own_session,
                permit,
                current_time=current_time,
                extend_lease_seconds=extend_lease_seconds,
                correlation_id=correlation_id,
                commit_on_expire=True,
            )
            await own_session.commit()

    async def _mark_dispatched_in_session(
        self,
        session: AsyncSession,
        permit: MutationPermit,
        *,
        current_time: datetime,
        extend_lease_seconds: float,
        correlation_id: str,
        commit_on_expire: bool,
    ) -> None:
        epoch = (
            await session.execute(
                select(ResourceEpoch)
                .where(
                    ResourceEpoch.tenant_id == permit.tenant_id,
                    ResourceEpoch.target_id == permit.target_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()

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

        latest_fence = epoch.latest_fence if epoch is not None else 0
        if epoch is None or res is None:
            metrics.inc("fence_stale_rejects_total")
            emit_event(
                "fence.stale_reject",
                incident_id=permit.incident_id,
                correlation_id=correlation_id,
                target_id=permit.target_id,
                fence=permit.fence,
                latest_fence=latest_fence,
                reason="reservation_not_found",
                level="ERROR",
            )
            raise StaleFenceError(
                permit.target_id, permit.fence, latest_fence, "Reservation not found"
            )

        if (
            res.target_id != permit.target_id
            or res.holder_incident_id != permit.incident_id
            or res.action_digest != permit.action_digest
            or res.fence != permit.fence
        ):
            metrics.inc("fence_stale_rejects_total")
            emit_event(
                "fence.stale_reject",
                incident_id=permit.incident_id,
                correlation_id=correlation_id,
                target_id=permit.target_id,
                fence=permit.fence,
                latest_fence=epoch.latest_fence,
                reason="permit_field_mismatch",
                level="ERROR",
            )
            raise StaleFenceError(
                permit.target_id, permit.fence, epoch.latest_fence, "Permit field mismatch"
            )

        if res.expires_at <= current_time and res.state in (
            ReservationState.RESERVED.value,
            ReservationState.DISPATCHED.value,
        ):
            res.state = ReservationState.EXPIRED.value
            metrics.inc("lease_expired_total")
            emit_event(
                "lease.expired",
                incident_id=permit.incident_id,
                correlation_id=correlation_id,
                target_id=permit.target_id,
                expired_fence=permit.fence,
                level="WARNING",
            )
            if commit_on_expire:
                await session.commit()

        if res.state == ReservationState.EXPIRED.value or permit.fence < epoch.latest_fence:
            metrics.inc("fence_stale_rejects_total")
            emit_event(
                "fence.stale_reject",
                incident_id=permit.incident_id,
                correlation_id=correlation_id,
                target_id=permit.target_id,
                fence=permit.fence,
                latest_fence=epoch.latest_fence,
                reservation_state=res.state,
                level="WARNING",
            )
            raise ReservationExpiredError(
                permit.target_id,
                permit.fence,
                epoch.latest_fence,
                f"Reservation state is {res.state}",
            )

        if epoch.version_seen is not None and epoch.version_seen != permit.expected_version:
            metrics.inc("target_version_conflicts_total")
            emit_event(
                "target.version_conflict",
                incident_id=permit.incident_id,
                correlation_id=correlation_id,
                target_id=permit.target_id,
                expected_version=permit.expected_version,
                observed_version=epoch.version_seen,
                level="WARNING",
            )
            raise TargetVersionConflictError(
                permit.target_id, permit.expected_version, epoch.version_seen
            )

        current_state = ReservationState(res.state)
        if current_state == ReservationState.RESERVED:
            validate_reservation_transition(current_state, ReservationState.DISPATCHED)
            res.state = ReservationState.DISPATCHED.value
        elif current_state != ReservationState.DISPATCHED:
            raise StaleFenceError(
                permit.target_id,
                permit.fence,
                epoch.latest_fence,
                f"Cannot dispatch from state {current_state}",
            )

        res.expires_at = max(res.expires_at, current_time + timedelta(seconds=extend_lease_seconds))

    async def release_reservation(
        self,
        permit: MutationPermit,
        *,
        observed_version: str | None = None,
        session: AsyncSession | None = None,
        now: datetime | None = None,
    ) -> None:
        """Transition a reservation to RELEASED and update `resource_epoch.version_seen`
        if `permit.fence == epoch.latest_fence`.
        """
        if session is not None:
            await self._release_in_session(
                session, permit, observed_version=observed_version, now=now
            )
            return
        async with self._session_factory() as own_session:
            await self._release_in_session(
                own_session, permit, observed_version=observed_version, now=now
            )
            await own_session.commit()

    async def _release_in_session(
        self,
        session: AsyncSession,
        permit: MutationPermit,
        *,
        observed_version: str | None = None,
        now: datetime | None = None,
    ) -> None:
        _ = now
        epoch = (
            await session.execute(
                select(ResourceEpoch)
                .where(
                    ResourceEpoch.tenant_id == permit.tenant_id,
                    ResourceEpoch.target_id == permit.target_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()

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

        if res is not None:
            cur = ReservationState(res.state)
            if cur in (ReservationState.RESERVED, ReservationState.DISPATCHED):
                validate_reservation_transition(cur, ReservationState.RELEASED)
                res.state = ReservationState.RELEASED.value

        if (
            epoch is not None
            and observed_version is not None
            and permit.fence >= epoch.latest_fence
        ):
            epoch.version_seen = observed_version

    async def cancel_reservation(self, permit: MutationPermit) -> None:
        """Cancel a RESERVED reservation before dispatch."""
        async with self._session_factory() as session:
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
            if res is None:
                return
            cur = ReservationState(res.state)
            if cur == ReservationState.RESERVED:
                validate_reservation_transition(cur, ReservationState.CANCELLED)
                res.state = ReservationState.CANCELLED.value
                await session.commit()
