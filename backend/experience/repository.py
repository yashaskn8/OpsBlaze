"""Persistence repository for OpsBlaze Feature E1 — Experience Capsule."""

from __future__ import annotations

import base64
import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.domain.approval import validate_reviewer_role
from backend.domain.experience import (
    CapsuleNotFoundError,
    CapsuleStateConflictError,
    ConsentDeniedError,
    ExperienceAuditAction,
    ExperienceCapsuleResponse,
    ExperienceClassification,
    ExperienceExportResponse,
    ExperienceListResponse,
    ExperienceState,
)
from backend.observability.metrics import emit_event, metrics
from backend.persistence.models import ConsentEpoch, ExperienceAudit, ExperienceCapsule

logger = logging.getLogger("opsblaze.experience.repository")


def _to_response(row: ExperienceCapsule) -> ExperienceCapsuleResponse:
    return ExperienceCapsuleResponse(
        id=row.id,
        tenant_id=row.tenant_id,
        incident_id=row.incident_id,
        step_id=row.step_id,
        schema_version=row.schema_version,
        consent_epoch=row.consent_epoch,
        policy_bundle_version=row.policy_bundle_version,
        model_id=row.model_id,
        model_version=row.model_version,
        evidence_manifest_sha256=row.evidence_manifest_sha256,
        effect_receipt_id=row.effect_receipt_id,
        outcome_id=row.outcome_id,
        outcome_revision_no=row.outcome_revision_no,
        redaction_version=row.redaction_version,
        redacted_digest=row.redacted_digest,
        summary_markdown=row.summary_markdown,
        classification=ExperienceClassification(row.classification),
        lifecycle_state=ExperienceState(row.lifecycle_state),
        occurred_at=row.occurred_at,
        expires_at=row.expires_at,
        created_at=row.created_at,
    )


class ExperienceRepository:
    """Manages experience capsule persistence, keyset querying, quarantine, and tombstones."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list_eligible_experiences(
        self,
        tenant_id: uuid.UUID,
        *,
        cursor: str | None = None,
        limit: int = 20,
    ) -> ExperienceListResponse:
        """Query-time consent check and keyset pagination for eligible capsules."""
        async with self._session_factory() as session:
            # 1. Total eligible count with live consent check
            count_stmt = (
                select(func.count(ExperienceCapsule.id))
                .join(ConsentEpoch, ExperienceCapsule.tenant_id == ConsentEpoch.tenant_id)
                .where(
                    ExperienceCapsule.tenant_id == tenant_id,
                    ConsentEpoch.reuse_allowed.is_(True),
                    ExperienceCapsule.consent_epoch == ConsentEpoch.current_epoch,
                    ExperienceCapsule.lifecycle_state == ExperienceState.ELIGIBLE.value,
                )
            )
            total = (await session.execute(count_stmt)).scalar() or 0

            if total == 0:
                metrics.inc("experience_query_denied_total")
                return ExperienceListResponse(items=[], next_cursor=None, total_eligible=0)

            # 2. Keyset query
            stmt = (
                select(ExperienceCapsule)
                .join(ConsentEpoch, ExperienceCapsule.tenant_id == ConsentEpoch.tenant_id)
                .where(
                    ExperienceCapsule.tenant_id == tenant_id,
                    ConsentEpoch.reuse_allowed.is_(True),
                    ExperienceCapsule.consent_epoch == ConsentEpoch.current_epoch,
                    ExperienceCapsule.lifecycle_state == ExperienceState.ELIGIBLE.value,
                )
            )

            if cursor:
                try:
                    raw_cursor = base64.b64decode(cursor).decode("utf-8")
                    c_iso, c_id_str = raw_cursor.split("|")
                    c_time = datetime.fromisoformat(c_iso)
                    c_id = uuid.UUID(c_id_str)
                    stmt = stmt.where(
                        (ExperienceCapsule.occurred_at < c_time)
                        | (
                            (ExperienceCapsule.occurred_at == c_time)
                            & (ExperienceCapsule.id < c_id)
                        )
                    )
                except (ValueError, TypeError, KeyError) as exc:
                    logger.warning("Invalid experience cursor %s: %s", cursor, exc)

            stmt = stmt.order_by(
                ExperienceCapsule.occurred_at.desc(), ExperienceCapsule.id.desc()
            ).limit(limit + 1)

            rows = (await session.execute(stmt)).scalars().all()

            has_more = len(rows) > limit
            page_rows = rows[:limit]

            next_cursor = None
            if has_more and page_rows:
                last = page_rows[-1]
                raw_next = f"{last.occurred_at.isoformat()}|{last.id}"
                next_cursor = base64.b64encode(raw_next.encode("utf-8")).decode("utf-8")

            metrics.inc("experience_capsule_eligible_total", len(page_rows))
            return ExperienceListResponse(
                items=[_to_response(r) for r in page_rows],
                next_cursor=next_cursor,
                total_eligible=total,
            )

    async def get_capsule(
        self, tenant_id: uuid.UUID, capsule_id: uuid.UUID
    ) -> ExperienceCapsuleResponse | None:
        """Fetch a single capsule by tenant and ID."""
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(ExperienceCapsule).where(
                        ExperienceCapsule.tenant_id == tenant_id,
                        ExperienceCapsule.id == capsule_id,
                    )
                )
            ).scalar_one_or_none()
            return _to_response(row) if row else None

    async def quarantine_capsule(
        self,
        tenant_id: uuid.UUID,
        capsule_id: uuid.UUID,
        *,
        actor_id: str,
        actor_role: str,
        reason: str,
        expected_state: ExperienceState = ExperienceState.ELIGIBLE,
        now: datetime | None = None,
    ) -> ExperienceCapsuleResponse:
        """Quarantine a capsule under reviewer authority with optimistic concurrency."""
        validate_reviewer_role(actor_role)
        current_time = now or datetime.now(UTC)

        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(ExperienceCapsule)
                    .where(
                        ExperienceCapsule.tenant_id == tenant_id,
                        ExperienceCapsule.id == capsule_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()

            if row is None:
                raise CapsuleNotFoundError(capsule_id)

            if row.lifecycle_state != expected_state.value:
                raise CapsuleStateConflictError(expected_state.value, row.lifecycle_state)

            row.lifecycle_state = ExperienceState.QUARANTINED.value
            row.updated_at = current_time

            session.add(
                ExperienceAudit(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    capsule_id=capsule_id,
                    action=ExperienceAuditAction.QUARANTINED.value,
                    actor_id=actor_id,
                    actor_role=actor_role,
                    reason=reason,
                    timestamp=current_time,
                )
            )
            await session.commit()

        emit_event(
            "experience.quarantined",
            tenant_id=str(tenant_id),
            capsule_id=str(capsule_id),
            actor_id=actor_id,
        )
        return _to_response(row)

    async def export_experiences(
        self,
        tenant_id: uuid.UUID,
        *,
        max_records: int = 1000,
    ) -> ExperienceExportResponse:
        """Bounded export of authorized redacted experiences for a tenant."""
        now = datetime.now(UTC)
        async with self._session_factory() as session:
            # Check consent
            consent = (
                await session.execute(
                    select(ConsentEpoch).where(ConsentEpoch.tenant_id == tenant_id)
                )
            ).scalar_one_or_none()

            if consent is None or not consent.reuse_allowed:
                raise ConsentDeniedError(tenant_id)

            stmt = (
                select(ExperienceCapsule)
                .where(
                    ExperienceCapsule.tenant_id == tenant_id,
                    ExperienceCapsule.consent_epoch == consent.current_epoch,
                    ExperienceCapsule.lifecycle_state == ExperienceState.ELIGIBLE.value,
                )
                .order_by(ExperienceCapsule.occurred_at.asc())
                .limit(max_records)
            )
            rows = (await session.execute(stmt)).scalars().all()

            return ExperienceExportResponse(
                tenant_id=tenant_id,
                exported_at=now,
                consent_epoch=consent.current_epoch,
                count=len(rows),
                capsules=[_to_response(r) for r in rows],
            )
