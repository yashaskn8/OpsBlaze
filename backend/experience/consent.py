"""Consent Epoch Management for OpsBlaze Feature E1 — Experience Capsule."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.domain.approval import validate_reviewer_role
from backend.domain.experience import (
    ConsentStatusResponse,
    ExperienceAuditAction,
)
from backend.observability.metrics import emit_event, metrics
from backend.persistence.models import ConsentEpoch, ExperienceAudit

logger = logging.getLogger("opsblaze.experience.consent")


class ConsentManager:
    """Manages tenant experience consent epochs and revocations."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_consent(self, tenant_id: uuid.UUID) -> ConsentStatusResponse:
        """Query current tenant consent status. Defaults to reuse denied."""
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(ConsentEpoch).where(ConsentEpoch.tenant_id == tenant_id)
                )
            ).scalar_one_or_none()

            if row is None:
                # Default to reuse denied where no explicit consent record exists
                return ConsentStatusResponse(
                    tenant_id=tenant_id,
                    current_epoch=1,
                    reuse_allowed=False,
                    updated_at=datetime.now(UTC),
                    revoked_at=None,
                )

            return ConsentStatusResponse(
                tenant_id=row.tenant_id,
                current_epoch=row.current_epoch,
                reuse_allowed=row.reuse_allowed,
                updated_at=row.updated_at,
                revoked_at=row.revoked_at,
            )

    async def grant_consent(
        self,
        tenant_id: uuid.UUID,
        *,
        actor_id: str,
        actor_role: str,
        reason: str,
        now: datetime | None = None,
    ) -> ConsentStatusResponse:
        """Grant or update tenant experience reuse consent."""
        validate_reviewer_role(actor_role)
        current_time = now or datetime.now(UTC)

        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(ConsentEpoch)
                    .where(ConsentEpoch.tenant_id == tenant_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()

            if row is None:
                row = ConsentEpoch(
                    tenant_id=tenant_id,
                    current_epoch=1,
                    reuse_allowed=True,
                    updated_at=current_time,
                    revoked_at=None,
                )
                session.add(row)
            else:
                row.reuse_allowed = True
                row.updated_at = current_time
                row.revoked_at = None

            # Audit record
            session.add(
                ExperienceAudit(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    capsule_id=uuid.UUID("00000000-0000-0000-0000-000000000000"),
                    action=ExperienceAuditAction.CONSENT_GRANTED.value,
                    actor_id=actor_id,
                    actor_role=actor_role,
                    reason=reason,
                    timestamp=current_time,
                )
            )
            await session.commit()

        emit_event(
            "experience.consent.granted",
            tenant_id=str(tenant_id),
            epoch=row.current_epoch,
            actor_id=actor_id,
        )
        metrics.inc("experience_consent_granted_total")

        return ConsentStatusResponse(
            tenant_id=row.tenant_id,
            current_epoch=row.current_epoch,
            reuse_allowed=row.reuse_allowed,
            updated_at=row.updated_at,
            revoked_at=row.revoked_at,
        )

    async def revoke_consent(
        self,
        tenant_id: uuid.UUID,
        *,
        actor_id: str,
        actor_role: str,
        reason: str,
        now: datetime | None = None,
    ) -> ConsentStatusResponse:
        """Atomically advances tenant consent epoch and sets reuse_allowed = False."""
        validate_reviewer_role(actor_role)
        current_time = now or datetime.now(UTC)

        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(ConsentEpoch)
                    .where(ConsentEpoch.tenant_id == tenant_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()

            if row is None:
                # If revoking when no prior record, advance to epoch 2 and deny
                row = ConsentEpoch(
                    tenant_id=tenant_id,
                    current_epoch=2,
                    reuse_allowed=False,
                    updated_at=current_time,
                    revoked_at=current_time,
                )
                session.add(row)
            else:
                row.current_epoch += 1
                row.reuse_allowed = False
                row.revoked_at = current_time
                row.updated_at = current_time

            session.add(
                ExperienceAudit(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    capsule_id=uuid.UUID("00000000-0000-0000-0000-000000000000"),
                    action=ExperienceAuditAction.CONSENT_REVOKED.value,
                    actor_id=actor_id,
                    actor_role=actor_role,
                    reason=reason,
                    timestamp=current_time,
                )
            )
            await session.commit()

        emit_event(
            "experience.consent.revoked",
            tenant_id=str(tenant_id),
            new_epoch=row.current_epoch,
            actor_id=actor_id,
        )
        metrics.inc("experience_consent_revoked_total")

        return ConsentStatusResponse(
            tenant_id=row.tenant_id,
            current_epoch=row.current_epoch,
            reuse_allowed=row.reuse_allowed,
            updated_at=row.updated_at,
            revoked_at=row.revoked_at,
        )
