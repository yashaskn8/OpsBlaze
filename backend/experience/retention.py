"""Retention and asynchronous purge worker for OpsBlaze Feature E1 — Experience Capsule."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.domain.approval import validate_reviewer_role
from backend.domain.experience import (
    CapsuleNotFoundError,
    ExperienceAuditAction,
    ExperienceState,
)
from backend.observability.metrics import emit_event, metrics
from backend.persistence.models import ConsentEpoch, ExperienceAudit, ExperienceCapsule

logger = logging.getLogger("opsblaze.experience.retention")


class RetentionWorker:
    """Handles TTL expiration purging and consent revocation background purging for E1 capsules."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def purge_expired_capsules(
        self,
        *,
        batch_size: int = 100,
        now: datetime | None = None,
    ) -> int:
        """Purge payloads of capsules that have exceeded their TTL (expires_at <= now)."""
        current_time = now or datetime.now(UTC)
        purged_count = 0

        async with self._session_factory() as session:
            stmt = (
                select(ExperienceCapsule)
                .where(
                    ExperienceCapsule.expires_at <= current_time,
                    ExperienceCapsule.lifecycle_state != ExperienceState.PURGED.value,
                )
                .with_for_update(skip_locked=True)
                .limit(batch_size)
            )
            rows = (await session.execute(stmt)).scalars().all()

            for capsule in rows:
                capsule.lifecycle_state = ExperienceState.PURGED.value
                capsule.summary_markdown = None
                capsule.updated_at = current_time
                session.add(
                    ExperienceAudit(
                        id=uuid.uuid4(),
                        tenant_id=capsule.tenant_id,
                        capsule_id=capsule.id,
                        action=ExperienceAuditAction.PURGED.value,
                        actor_id="system:retention_worker",
                        actor_role="system",
                        reason="Retention TTL expired",
                        timestamp=current_time,
                    )
                )
                purged_count += 1

            if purged_count > 0:
                await session.commit()
                metrics.inc("experience_capsule_purged_total", purged_count)
                logger.info("Purged %d expired experience capsules", purged_count)

        return purged_count

    async def purge_revoked_capsules(
        self,
        tenant_id: uuid.UUID,
        *,
        batch_size: int = 100,
        now: datetime | None = None,
    ) -> int:
        """Purge payloads of capsules invalidated by tenant consent revocation or epoch advance."""
        current_time = now or datetime.now(UTC)
        purged_count = 0

        async with self._session_factory() as session:
            # 1. Fetch live tenant consent epoch
            consent = (
                await session.execute(
                    select(ConsentEpoch).where(ConsentEpoch.tenant_id == tenant_id)
                )
            ).scalar_one_or_none()

            if consent is None:
                return 0

            # 2. Select invalid capsules: either reuse is disallowed, or capsule epoch < current epoch
            stmt = (
                select(ExperienceCapsule)
                .where(
                    ExperienceCapsule.tenant_id == tenant_id,
                    ExperienceCapsule.lifecycle_state != ExperienceState.PURGED.value,
                )
            )
            if not consent.reuse_allowed:
                # All non-purged capsules must be purged
                pass
            else:
                stmt = stmt.where(ExperienceCapsule.consent_epoch < consent.current_epoch)

            stmt = stmt.with_for_update(skip_locked=True).limit(batch_size)
            rows = (await session.execute(stmt)).scalars().all()

            for capsule in rows:
                capsule.lifecycle_state = ExperienceState.PURGED.value
                capsule.summary_markdown = None
                capsule.updated_at = current_time
                session.add(
                    ExperienceAudit(
                        id=uuid.uuid4(),
                        tenant_id=tenant_id,
                        capsule_id=capsule.id,
                        action=ExperienceAuditAction.PURGED.value,
                        actor_id="system:retention_worker",
                        actor_role="system",
                        reason="Consent revoked or epoch superseded",
                        timestamp=current_time,
                    )
                )
                purged_count += 1

            if purged_count > 0:
                await session.commit()
                metrics.inc("experience_capsule_purged_total", purged_count)
                logger.info(
                    "Purged %d revoked experience capsules for tenant %s",
                    purged_count,
                    tenant_id,
                )

        return purged_count

    async def tombstone_capsule(
        self,
        tenant_id: uuid.UUID,
        capsule_id: uuid.UUID,
        *,
        actor_id: str,
        actor_role: str,
        reason: str,
        now: datetime | None = None,
    ) -> None:
        """Explicitly tombstone a single capsule, erasing its payload summary."""
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

            row.lifecycle_state = ExperienceState.TOMBSTONED.value
            row.summary_markdown = None
            row.updated_at = current_time

            session.add(
                ExperienceAudit(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    capsule_id=capsule_id,
                    action=ExperienceAuditAction.TOMBSTONED.value,
                    actor_id=actor_id,
                    actor_role=actor_role,
                    reason=reason,
                    timestamp=current_time,
                )
            )
            await session.commit()

        emit_event(
            "experience.tombstoned",
            tenant_id=str(tenant_id),
            capsule_id=str(capsule_id),
            actor_id=actor_id,
        )
