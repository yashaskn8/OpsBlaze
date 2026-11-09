"""Tenant-scoped PostgreSQL repositories for B1 incident admission, status projection, and outbox."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

logger = logging.getLogger('opsblaze.persistence.repositories')
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.domain.incident import (
    TERMINAL_STATUSES,
    IdempotencyConflictError,
    IncidentNotFoundError,
    IncidentResponse,
    IncidentStatus,
    InvalidStateTransitionError,
    StaleStateVersionError,
    make_incident_workflow_id,
    validate_client_key,
    validate_transition,
)
from backend.observability.metrics import emit_event, metrics
from backend.persistence.models import Incident, Outbox, Tenant

OUTBOX_KIND_INCIDENT_START = "INCIDENT_START"
OUTBOX_KIND_GUARDRAIL = "RELEASE_GUARDRAIL"
OUTBOX_KIND_SHADOW = "RELEASE_SHADOW_EVALUATION"


@dataclass(frozen=True)
class ClaimedGuardrailOutboxItem:
    outbox_id: int
    tenant_id: uuid.UUID
    release_id: uuid.UUID
    incident_id: uuid.UUID | None
    attempt: int
    payload: dict[str, Any]
    available_at: datetime


@dataclass(frozen=True)
class ClaimedShadowOutboxItem:
    outbox_id: int
    tenant_id: uuid.UUID
    release_id: uuid.UUID
    incident_id: uuid.UUID | None
    attempt: int
    payload: dict[str, Any]
    available_at: datetime


@dataclass(frozen=True)
class AdmissionOutcome:
    response: IncidentResponse
    created: bool


@dataclass(frozen=True)
class ClaimedOutboxItem:
    outbox_id: int
    incident_id: uuid.UUID
    tenant_id: uuid.UUID
    target_id: str
    request_sha256: str
    incident_status: IncidentStatus
    incident_status_version: int
    attempt: int
    created_at: datetime
    available_at: datetime


class IncidentRepository:
    """Tenant-scoped incident persistence and state projection repository."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def ensure_tenant(self, tenant_id: uuid.UUID, name: str | None = None) -> Tenant:
        """Idempotently ensure a tenant row exists in a short transaction."""
        async with self._session_factory() as session:
            existing = await session.get(Tenant, tenant_id)
            if existing is not None:
                return existing
            tenant_name = name or f"tenant-{tenant_id}"
            tenant = Tenant(id=tenant_id, name=tenant_name)
            session.add(tenant)
            try:
                await session.commit()
                return tenant
            except IntegrityError:
                await session.rollback()
                found = await session.get(Tenant, tenant_id)
                if found is not None:
                    return found
                raise

    async def tenant_exists(self, tenant_id: uuid.UUID) -> bool:
        async with self._session_factory() as session:
            row = (
                await session.execute(select(Tenant.id).where(Tenant.id == tenant_id))
            ).scalar_one_or_none()
            return row is not None

    async def admit_incident(
        self,
        *,
        tenant_id: uuid.UUID,
        client_key: str,
        target_id: str,
        request_sha256: str,
        correlation_id: str = "",
        pinned_policy_bundle_id: uuid.UUID | None = None,
        policy_release_id: uuid.UUID | None = None,
        canary_arm: str | None = None,
        assignment_hash_val: int | None = None,
    ) -> AdmissionOutcome:
        """Atomically admit a tenant-scoped incident and its outbox start intent.

        Guarantees:
        1. Incident and Outbox commit in a single short PostgreSQL transaction.
        2. No network/Temporal call happens inside the database transaction.
        3. Same (tenant_id, client_key) + same request_sha256 returns the existing incident.
        4. Same (tenant_id, client_key) + different request_sha256 raises IdempotencyConflictError.
        """
        normalized_key = validate_client_key(client_key)
        metrics.inc("admission_requests_total")

        async with self._session_factory() as session:
            # First check if (tenant_id, client_key) already exists to avoid unnecessary exceptions
            existing = await self._load_by_client_key(session, tenant_id, normalized_key)
            if existing is not None:
                return await self._resolve_existing_admission(
                    session, existing, normalized_key, request_sha256
                )

            incident_id = uuid.uuid4()
            now = datetime.now(UTC)
            incident = Incident(
                id=incident_id,
                tenant_id=tenant_id,
                client_key=normalized_key,
                request_sha256=request_sha256,
                target_id=target_id,
                status_projection=IncidentStatus.ACCEPTED.value,
                status_version=1,
                created_at=now,
                pinned_policy_bundle_id=pinned_policy_bundle_id,
                policy_release_id=policy_release_id,
                canary_arm=canary_arm,
                assignment_hash_val=assignment_hash_val,
            )
            outbox = Outbox(
                incident_id=incident_id,
                kind=OUTBOX_KIND_INCIDENT_START,
                attempt=0,
                available_at=now,
                delivered_at=None,
            )
            session.add(incident)
            session.add(outbox)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                # Check if concurrent duplicate key on ux_incident_key won the race
                raced = await self._load_by_client_key(session, tenant_id, normalized_key)
                if raced is not None:
                    return await self._resolve_existing_admission(
                        session, raced, normalized_key, request_sha256
                    )
                raise

            workflow_id = make_incident_workflow_id(tenant_id, incident.id)
            metrics.inc("admission_created_total")
            emit_event(
                "incident.admitted",
                incident_id=incident.id,
                workflow_id=workflow_id,
                correlation_id=correlation_id,
                target_id=target_id,
                status=IncidentStatus.ACCEPTED.value,
                status_version=1,
            )
            response = IncidentResponse(
                id=incident.id,
                tenant_id=incident.tenant_id,
                client_key=incident.client_key,
                target_id=incident.target_id,
                request_sha256=incident.request_sha256,
                status=IncidentStatus(incident.status_projection),
                status_version=incident.status_version,
                workflow_id=workflow_id,
                created_at=incident.created_at,
                updated_at=incident.created_at,
                outbox_delivered=False,
                outbox_attempts=0,
                projection_lag_ms=0.0,
                projection_stale=False,
            )
            return AdmissionOutcome(response=response, created=True)

    async def _load_by_client_key(
        self, session: AsyncSession, tenant_id: uuid.UUID, client_key: str
    ) -> Incident | None:
        stmt = select(Incident).where(
            Incident.tenant_id == tenant_id,
            Incident.client_key == client_key,
        )
        return (await session.execute(stmt)).scalar_one_or_none()

    async def _resolve_existing_admission(
        self,
        session: AsyncSession,
        existing: Incident,
        client_key: str,
        request_sha256: str,
    ) -> AdmissionOutcome:
        if existing.request_sha256 != request_sha256:
            metrics.inc("idempotency_conflicts_total")
            raise IdempotencyConflictError(client_key)
        metrics.inc("admission_replayed_total")
        resp = await self._build_incident_response(session, existing)
        return AdmissionOutcome(response=resp, created=False)

    async def get_incident(self, tenant_id: uuid.UUID, incident_id: uuid.UUID) -> IncidentResponse:
        """Fetch a tenant-scoped incident snapshot and outbox projection metadata."""
        async with self._session_factory() as session:
            stmt = select(Incident).where(
                Incident.tenant_id == tenant_id,
                Incident.id == incident_id,
            )
            incident = (await session.execute(stmt)).scalar_one_or_none()
            if incident is None:
                raise IncidentNotFoundError(incident_id)
            return await self._build_incident_response(session, incident)

    async def _build_incident_response(
        self, session: AsyncSession, incident: Incident
    ) -> IncidentResponse:
        outbox_stmt = (
            select(Outbox)
            .where(Outbox.incident_id == incident.id)
            .order_by(Outbox.id.asc())
            .limit(1)
        )
        outbox = (await session.execute(outbox_stmt)).scalar_one_or_none()
        now = datetime.now(UTC)
        delivered = outbox is not None and outbox.delivered_at is not None
        attempts = outbox.attempt if outbox is not None else 0
        updated_at = (
            outbox.delivered_at
            if (outbox is not None and outbox.delivered_at is not None)
            else incident.created_at
        )

        # Compute projection lag: if outbox has not yet delivered or workflow hasn't advanced
        status_enum = IncidentStatus(incident.status_projection)
        if not delivered and status_enum == IncidentStatus.ACCEPTED:
            lag_seconds = max(0.0, (now - incident.created_at).total_seconds())
        elif (
            delivered
            and status_enum == IncidentStatus.DISPATCH_PENDING
            and outbox
            and outbox.delivered_at
        ):
            lag_seconds = max(0.0, (now - outbox.delivered_at).total_seconds())
        else:
            lag_seconds = 0.0

        lag_ms = round(lag_seconds * 1000.0, 2)
        metrics.set_gauge("projection_lag_seconds", lag_seconds)
        stale = (not delivered and lag_seconds > 10.0) or (attempts >= 3 and not delivered)

        return IncidentResponse(
            id=incident.id,
            tenant_id=incident.tenant_id,
            client_key=incident.client_key,
            target_id=incident.target_id,
            request_sha256=incident.request_sha256,
            status=status_enum,
            status_version=incident.status_version,
            workflow_id=make_incident_workflow_id(incident.tenant_id, incident.id),
            created_at=incident.created_at,
            updated_at=updated_at,
            outbox_delivered=delivered,
            outbox_attempts=attempts,
            projection_lag_ms=lag_ms,
            projection_stale=stale,
        )

    async def transition_status(
        self,
        *,
        tenant_id: uuid.UUID,
        incident_id: uuid.UUID,
        target_status: IncidentStatus,
        expected_version: int | None = None,
        allow_cancelled_noop: bool = False,
        correlation_id: str = "",
    ) -> IncidentResponse:
        """Atomically validate and apply a versioned B1 status transition in PostgreSQL."""
        async with self._session_factory() as session:
            stmt = (
                select(Incident)
                .where(
                    Incident.tenant_id == tenant_id,
                    Incident.id == incident_id,
                )
                .with_for_update()
            )
            incident = (await session.execute(stmt)).scalar_one_or_none()
            if incident is None:
                raise IncidentNotFoundError(incident_id)

            current_status = IncidentStatus(incident.status_projection)
            current_version = incident.status_version

            # If an activity or dispatcher attempts to advance an incident that was already
            # cancelled concurrently, honor the terminal cancellation without overwriting it.
            if (
                allow_cancelled_noop
                and current_status == IncidentStatus.CANCELLED
                and target_status != IncidentStatus.CANCELLED
            ):
                return await self._build_incident_response(session, incident)

            # Allow workflow activity to advance ACCEPTED -> INVESTIGATING directly if the
            # worker executes the first activity before the dispatcher's outbox-ack commits,
            # or if expected_version was omitted by an internal activity transition.
            effective_expected = expected_version
            if (
                current_status == IncidentStatus.ACCEPTED
                and target_status == IncidentStatus.INVESTIGATING
                and allow_cancelled_noop
            ):
                # Step through DISPATCH_PENDING -> INVESTIGATING atomically
                validate_transition(
                    current_status,
                    IncidentStatus.DISPATCH_PENDING,
                    current_version,
                    effective_expected,
                )
                incident.status_projection = IncidentStatus.INVESTIGATING.value
                incident.status_version = current_version + 1
            else:
                should_apply = validate_transition(
                    current_status=current_status,
                    target_status=target_status,
                    current_version=current_version,
                    expected_version=effective_expected,
                )
                if not should_apply:
                    return await self._build_incident_response(session, incident)

                # Guard CAS with explicit UPDATE ... WHERE status_version = current_version
                upd = (
                    update(Incident)
                    .where(
                        Incident.tenant_id == tenant_id,
                        Incident.id == incident_id,
                        Incident.status_version == current_version,
                    )
                    .values(
                        status_projection=target_status.value,
                        status_version=current_version + 1,
                    )
                )
                res = await session.execute(upd)
                if res.rowcount != 1:  # type: ignore[attr-defined]
                    await session.rollback()
                    raise StaleStateVersionError(
                        current_version=current_version,
                        expected_version=effective_expected or current_version,
                    )
                incident.status_projection = target_status.value
                incident.status_version = current_version + 1

            # If cancelling, also mark any undelivered incident_start outbox entry as delivered/superseded
            # in the same short transaction so the dispatcher will not start a workflow later.
            if target_status == IncidentStatus.CANCELLED:
                now = datetime.now(UTC)
                await session.execute(
                    update(Outbox)
                    .where(
                        Outbox.incident_id == incident_id,
                        Outbox.kind.in_(("INCIDENT_START", "incident_start")),
                        Outbox.delivered_at.is_(None),
                    )
                    .values(delivered_at=now)
                )

            # Persist the guardrail evaluation request in the same transaction as the
            # terminal incident transition. The independent dispatcher retries after crashes.
            if (
                target_status in (
                    IncidentStatus.VERIFIED,
                    IncidentStatus.FAILED,
                    IncidentStatus.NEEDS_REVIEW,
                    IncidentStatus.CANCELLED,
                )
                and incident.policy_release_id is not None
            ):
                guardrail_dedupe = f"guardrail:{incident.id}:{incident.status_version}"
                check_stmt = select(Outbox.id).where(
                    Outbox.kind == "RELEASE_GUARDRAIL",
                    Outbox.dedupe_key == guardrail_dedupe,
                    Outbox.delivered_at.is_(None),
                ).limit(1)
                existing_guardrail = (await session.execute(check_stmt)).scalar_one_or_none()
                if existing_guardrail is None:
                    session.add(
                        Outbox(
                            incident_id=incident.id,
                            kind="RELEASE_GUARDRAIL",
                            dedupe_key=guardrail_dedupe,
                            payload={
                                "tenant_id": str(tenant_id),
                                "release_id": str(incident.policy_release_id),
                                "incident_id": str(incident.id),
                                "terminal_status": target_status.value,
                                "canary_arm": incident.canary_arm,
                            },
                            attempt=0,
                            available_at=datetime.now(UTC),
                        )
                    )

            await session.commit()

            if target_status == IncidentStatus.CANCELLED:
                metrics.inc("workflow_cancelled_total")
                emit_event(
                    "workflow.cancelled",
                    incident_id=incident.id,
                    workflow_id=make_incident_workflow_id(tenant_id, incident.id),
                    correlation_id=correlation_id,
                    previous_status=current_status.value,
                    status_version=incident.status_version,
                )
            elif target_status == IncidentStatus.FAILED:
                metrics.inc("workflow_failures_total")
                emit_event(
                    "workflow.failed",
                    incident_id=incident.id,
                    workflow_id=make_incident_workflow_id(tenant_id, incident.id),
                    correlation_id=correlation_id,
                    previous_status=current_status.value,
                    status_version=incident.status_version,
                    level="ERROR",
                )
            elif target_status in (IncidentStatus.VERIFIED, IncidentStatus.NEEDS_REVIEW):
                metrics.inc("workflow_completed_total")
                emit_event(
                    "workflow.completed",
                    incident_id=incident.id,
                    workflow_id=make_incident_workflow_id(tenant_id, incident.id),
                    correlation_id=correlation_id,
                    final_status=target_status.value,
                    status_version=incident.status_version,
                )

            return await self._build_incident_response(session, incident)

    async def reconcile_status_projection_forward(
        self,
        *,
        tenant_id: uuid.UUID,
        incident_id: uuid.UUID,
        target_status: IncidentStatus,
        mark_outbox_delivered: bool = True,
        correlation_id: str = "",
        now: datetime | None = None,
    ) -> IncidentResponse:
        """Monotonically advance `incident.status_projection` forward during cross-store recovery.

        Enforces strict lifecycle rank ordering, never regresses terminal states, increments
        `status_version`, and optionally marks undelivered outbox rows delivered.
        """
        lifecycle_rank: dict[IncidentStatus, int] = {
            IncidentStatus.ACCEPTED: 1,
            IncidentStatus.DISPATCH_PENDING: 2,
            IncidentStatus.INVESTIGATING: 3,
            IncidentStatus.AWAITING_APPROVAL: 4,
            IncidentStatus.EXECUTING: 5,
            IncidentStatus.VERIFYING: 6,
            IncidentStatus.VERIFIED: 7,
            IncidentStatus.FAILED: 7,
            IncidentStatus.NEEDS_REVIEW: 7,
            IncidentStatus.CANCELLED: 7,
        }
        current_time = now or datetime.now(UTC)

        async with self._session_factory() as session:
            incident = (
                await session.execute(
                    select(Incident)
                    .where(
                        Incident.tenant_id == tenant_id,
                        Incident.id == incident_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if incident is None:
                raise IncidentNotFoundError(incident_id)

            cur = IncidentStatus(incident.status_projection)
            if cur == target_status or cur in TERMINAL_STATUSES:
                if mark_outbox_delivered:
                    await session.execute(
                        update(Outbox)
                        .where(
                            Outbox.incident_id == incident_id,
                            Outbox.kind.in_(("INCIDENT_START", "incident_start")),
                            Outbox.delivered_at.is_(None),
                        )
                        .values(delivered_at=current_time)
                    )
                    await session.commit()
                return await self._build_incident_response(session, incident)

            if lifecycle_rank[target_status] < lifecycle_rank[cur]:
                raise InvalidStateTransitionError(
                    cur,
                    target_status,
                    "cannot regress incident status projection during recovery",
                )

            incident.status_projection = target_status.value
            incident.status_version += 1

            if mark_outbox_delivered or target_status in TERMINAL_STATUSES:
                await session.execute(
                    update(Outbox)
                    .where(
                        Outbox.incident_id == incident_id,
                        Outbox.kind.in_(("INCIDENT_START", "incident_start")),
                        Outbox.delivered_at.is_(None),
                    )
                    .values(delivered_at=current_time)
                )

            await session.commit()
            emit_event(
                "recovery.incident_status_reconciled",
                incident_id=incident.id,
                workflow_id=make_incident_workflow_id(tenant_id, incident.id),
                correlation_id=correlation_id,
                previous_status=cur.value,
                target_status=target_status.value,
                status_version=incident.status_version,
            )
            return await self._build_incident_response(session, incident)


class OutboxRepository:
    """Short-transaction outbox claiming and acknowledgment repository."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def claim_batch(
        self,
        *,
        kind: str = OUTBOX_KIND_INCIDENT_START,
        batch_size: int = 10,
        lease_seconds: float = 15.0,
        stuck_attempt_threshold: int = 5,
        stuck_age_seconds: float = 60.0,
        now: datetime | None = None,
    ) -> list[ClaimedOutboxItem]:
        """Claim eligible outbox rows in a short FOR UPDATE SKIP LOCKED transaction.

        Advances `available_at = now + lease_seconds` and increments `attempt` before
        committing, so no row lock or transaction is held across the Temporal RPC.
        """
        claim_time = now or datetime.now(UTC)
        lease_until = claim_time + timedelta(seconds=lease_seconds)

        async with self._session_factory() as session:
            stmt = (
                select(Outbox, Incident)
                .join(Incident, Outbox.incident_id == Incident.id)
                .where(
                    Outbox.kind.in_((kind, kind.lower(), kind.upper())),
                    Outbox.delivered_at.is_(None),
                    Outbox.available_at <= claim_time,
                )
                .order_by(Outbox.available_at.asc(), Outbox.id.asc())
                .limit(batch_size)
                .with_for_update(of=Outbox, skip_locked=True)
            )
            rows = (await session.execute(stmt)).all()
            if not rows:
                return []

            claimed: list[ClaimedOutboxItem] = []
            max_age = 0.0
            max_stuck_age = 0.0

            for outbox, incident in rows:
                outbox.attempt += 1
                outbox.available_at = lease_until
                age_sec = max(0.0, (claim_time - incident.created_at).total_seconds())
                max_age = max(max_age, age_sec)

                wf_id = make_incident_workflow_id(incident.tenant_id, incident.id)
                metrics.inc("outbox_claimed_total")
                emit_event(
                    "outbox.claimed",
                    incident_id=incident.id,
                    workflow_id=wf_id,
                    outbox_id=outbox.id,
                    attempt=outbox.attempt,
                    age_seconds=round(age_sec, 3),
                )

                if outbox.attempt >= stuck_attempt_threshold or age_sec >= stuck_age_seconds:
                    max_stuck_age = max(max_stuck_age, age_sec)
                    metrics.inc("outbox_stuck_total")
                    emit_event(
                        "outbox.stuck",
                        incident_id=incident.id,
                        workflow_id=wf_id,
                        outbox_id=outbox.id,
                        attempt=outbox.attempt,
                        age_seconds=round(age_sec, 3),
                        level="WARNING",
                    )

                claimed.append(
                    ClaimedOutboxItem(
                        outbox_id=outbox.id,
                        incident_id=incident.id,
                        tenant_id=incident.tenant_id,
                        target_id=incident.target_id,
                        request_sha256=incident.request_sha256,
                        incident_status=IncidentStatus(incident.status_projection),
                        incident_status_version=incident.status_version,
                        attempt=outbox.attempt,
                        created_at=incident.created_at,
                        available_at=lease_until,
                    )
                )

            metrics.set_gauge("outbox_age_seconds", max_age)
            if max_stuck_age > 0.0:
                metrics.set_gauge("stuck_workflow_age_seconds", max_stuck_age)

            await session.commit()
            return claimed

    async def mark_delivered(
        self,
        *,
        outbox_id: int,
        incident_id: uuid.UUID,
        tenant_id: uuid.UUID,
        now: datetime | None = None,
    ) -> bool:
        """Mark an outbox row delivered and advance ACCEPTED -> DISPATCH_PENDING atomically."""
        delivered_time = now or datetime.now(UTC)
        async with self._session_factory() as session:
            outbox = (
                await session.execute(
                    select(Outbox).where(Outbox.id == outbox_id).with_for_update()
                )
            ).scalar_one_or_none()
            if outbox is None:
                return False

            already_delivered = outbox.delivered_at is not None
            if not already_delivered:
                outbox.delivered_at = delivered_time

            incident = (
                await session.execute(
                    select(Incident)
                    .where(
                        Incident.tenant_id == tenant_id,
                        Incident.id == incident_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()

            if incident is not None and incident.status_projection == IncidentStatus.ACCEPTED.value:
                incident.status_projection = IncidentStatus.DISPATCH_PENDING.value
                incident.status_version += 1

            await session.commit()

            if not already_delivered:
                wf_id = make_incident_workflow_id(tenant_id, incident_id)
                sched_delay = (
                    max(0.0, (delivered_time - incident.created_at).total_seconds())
                    if incident is not None
                    else 0.0
                )
                metrics.inc("outbox_delivered_total")
                metrics.set_gauge("workflow_schedule_to_start_seconds", sched_delay)
                emit_event(
                    "outbox.delivered",
                    incident_id=incident_id,
                    workflow_id=wf_id,
                    outbox_id=outbox_id,
                    attempt=outbox.attempt,
                    schedule_to_start_seconds=round(sched_delay, 3),
                )
            return True

    async def mark_outbox_delivered(
        self,
        *,
        outbox_id: int,
        now: datetime | None = None,
    ) -> bool:
        """Mark an outbox row delivered without modifying incident status projections."""
        delivered_time = now or datetime.now(UTC)
        async with self._session_factory() as session:
            res = await session.execute(
                update(Outbox)
                .where(Outbox.id == outbox_id, Outbox.delivered_at.is_(None))
                .values(delivered_at=delivered_time, lease_owner=None)
            )
            await session.commit()
            return (res.rowcount or 0) > 0  # type: ignore[attr-defined]

    async def claim_guardrail_batch(
        self,
        *,
        batch_size: int = 10,
        lease_seconds: float = 15.0,
        now: datetime | None = None,
    ) -> list[ClaimedGuardrailOutboxItem]:
        """Claim eligible RELEASE_GUARDRAIL outbox rows via FOR UPDATE SKIP LOCKED."""
        claim_time = now or datetime.now(UTC)
        lease_until = claim_time + timedelta(seconds=lease_seconds)

        async with self._session_factory() as session:
            stmt = (
                select(Outbox)
                .where(
                    Outbox.kind == OUTBOX_KIND_GUARDRAIL,
                    Outbox.delivered_at.is_(None),
                    Outbox.available_at <= claim_time,
                )
                .order_by(Outbox.available_at.asc(), Outbox.id.asc())
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )
            rows = (await session.execute(stmt)).scalars().all()
            if not rows:
                return []

            claimed: list[ClaimedGuardrailOutboxItem] = []
            for outbox in rows:
                outbox.attempt += 1
                outbox.available_at = lease_until
                outbox.lease_owner = "guardrail-dispatcher"
                payload = outbox.payload or {}
                tenant_id = uuid.UUID(payload.get("tenant_id")) if payload.get("tenant_id") else uuid.UUID("00000000-0000-0000-0000-000000000000")
                release_id = uuid.UUID(payload.get("release_id")) if payload.get("release_id") else uuid.UUID("00000000-0000-0000-0000-000000000000")

                claimed.append(
                    ClaimedGuardrailOutboxItem(
                        outbox_id=outbox.id,
                        tenant_id=tenant_id,
                        release_id=release_id,
                        incident_id=outbox.incident_id,
                        attempt=outbox.attempt,
                        payload=payload,
                        available_at=lease_until,
                    )
                )
                metrics.inc("outbox_guardrail_claimed_total")

            await session.commit()
            return claimed

    async def claim_shadow_batch(
        self,
        *,
        batch_size: int = 10,
        lease_seconds: float = 30.0,
        now: datetime | None = None,
    ) -> list[ClaimedShadowOutboxItem]:
        """Claim eligible RELEASE_SHADOW_EVALUATION outbox rows via FOR UPDATE SKIP LOCKED."""
        claim_time = now or datetime.now(UTC)
        lease_until = claim_time + timedelta(seconds=lease_seconds)

        async with self._session_factory() as session:
            stmt = (
                select(Outbox)
                .where(
                    Outbox.kind == OUTBOX_KIND_SHADOW,
                    Outbox.delivered_at.is_(None),
                    Outbox.available_at <= claim_time,
                )
                .order_by(Outbox.available_at.asc(), Outbox.id.asc())
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )
            rows = (await session.execute(stmt)).scalars().all()
            if not rows:
                return []

            claimed: list[ClaimedShadowOutboxItem] = []
            for outbox in rows:
                outbox.attempt += 1
                outbox.available_at = lease_until
                outbox.lease_owner = "shadow-dispatcher"
                payload = outbox.payload or {}
                tenant_id = uuid.UUID(payload.get("tenant_id")) if payload.get("tenant_id") else uuid.UUID("00000000-0000-0000-0000-000000000000")
                release_id = uuid.UUID(payload.get("release_id")) if payload.get("release_id") else uuid.UUID("00000000-0000-0000-0000-000000000000")

                claimed.append(
                    ClaimedShadowOutboxItem(
                        outbox_id=outbox.id,
                        tenant_id=tenant_id,
                        release_id=release_id,
                        incident_id=outbox.incident_id,
                        attempt=outbox.attempt,
                        payload=payload,
                        available_at=lease_until,
                    )
                )
                metrics.inc("outbox_shadow_claimed_total")

            await session.commit()
            return claimed

    async def schedule_retry(
        self,
        *,
        outbox_id: int,
        incident_id: uuid.UUID | None = None,
        tenant_id: uuid.UUID | None = None,
        backoff_seconds: float,
        error_reason: str,
        now: datetime | None = None,
    ) -> None:
        """Reschedule a failed outbox delivery with bounded backoff and recorded last_error."""
        retry_base = now or datetime.now(UTC)
        next_available = retry_base + timedelta(seconds=max(0.05, backoff_seconds))
        sanitized_error = error_reason.replace("\r", " ").replace("\n", " ").strip()[:2048]
        async with self._session_factory() as session:
            await session.execute(
                update(Outbox)
                .where(Outbox.id == outbox_id, Outbox.delivered_at.is_(None))
                .values(
                    available_at=next_available,
                    last_error=sanitized_error,
                    lease_owner=None,
                )
            )
            await session.commit()

        if incident_id is not None and tenant_id is not None:
            wf_id = make_incident_workflow_id(tenant_id, incident_id)
            metrics.inc("outbox_retries_total")
            metrics.inc("dispatcher_failures_total")
            emit_event(
                "outbox.retry",
                incident_id=incident_id,
                workflow_id=wf_id,
                outbox_id=outbox_id,
                backoff_seconds=round(backoff_seconds, 3),
                error=sanitized_error,
                level="WARNING",
            )
