"""Operational Lesson Registry service managing drafting, reviewer validation, and revocation."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.domain.approval import validate_reviewer_role
from backend.domain.lesson import (
    LessonConfidenceBand,
    LessonLifecycle,
    LessonNotFoundError,
    LessonType,
    sanitize_lesson_summary_for_llm,
)
from backend.persistence.models import (
    ConsentEpoch,
    ExperienceCapsule,
    OperationalLesson,
    OutcomeRevision,
)


class LessonRegistry:
    """Manages the versioned registry of attributable operational lessons."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def draft_lesson(
        self,
        tenant_id: uuid.UUID,
        title: str,
        summary: str,
        source_outcome_revision_id: uuid.UUID,
        lesson_type: str = LessonType.VERIFIED_RECOVERY.value,
        applicability_predicate: dict[str, Any] | None = None,
        source_capsule_id: uuid.UUID | None = None,
        source_cluster_id: uuid.UUID | None = None,
        proposal_id: uuid.UUID | None = None,
        confidence_band: str = LessonConfidenceBand.MEDIUM.value,
        valid_days: int = 30,
        created_by: str = "human_operator",
        *,
        now: datetime | None = None,
    ) -> OperationalLesson:
        """Drafts an operational lesson from attributable E1/E2 sources with pre-validation."""
        current_time = now or datetime.now(UTC)
        pred = applicability_predicate or {}

        valid_types = {t.value for t in LessonType}
        if lesson_type not in valid_types:
            raise ValueError(f"Invalid lesson type '{lesson_type}'. Expected one of {valid_types}")

        async with self._session_factory() as session:
            # 1. Verify Tenant Consent Epoch
            epoch_row = await session.get(ConsentEpoch, tenant_id)
            if not epoch_row or not epoch_row.reuse_allowed:
                raise ValueError(
                    f"Tenant '{tenant_id}' has not consented to operational memory reuse."
                )
            current_epoch = epoch_row.current_epoch

            # 2. Verify Source Outcome Revision
            rev_row = await session.get(OutcomeRevision, source_outcome_revision_id)
            if not rev_row:
                raise ValueError(
                    f"Source outcome revision '{source_outcome_revision_id}' does not exist."
                )
            if rev_row.tenant_id != tenant_id:
                raise ValueError("Cross-tenant source outcome reference is strictly forbidden.")

            # Settled outcome requirement: cannot draft lessons from disputed/pending outcomes
            if rev_row.label in ("DISPUTED", "PENDING", "UNKNOWN"):
                raise ValueError(
                    f"Cannot draft lesson from unsettled outcome with label '{rev_row.label}'."
                )

            # Verification of type alignment
            if lesson_type == LessonType.VERIFIED_RECOVERY.value and rev_row.label != "VERIFIED_SUCCESS":
                raise ValueError("VERIFIED_RECOVERY lesson must reference a VERIFIED_SUCCESS outcome revision.")
            if lesson_type == LessonType.FAILURE_PATTERN.value and rev_row.label != "VERIFIED_FAILURE":
                raise ValueError("FAILURE_PATTERN lesson must reference a VERIFIED_FAILURE outcome revision.")

            # 3. Verify Source Capsule if provided
            if source_capsule_id:
                capsule_row = await session.get(ExperienceCapsule, source_capsule_id)
                if not capsule_row or capsule_row.tenant_id != tenant_id:
                    raise ValueError(f"Source capsule '{source_capsule_id}' is invalid or cross-tenant.")
                if capsule_row.consent_epoch != current_epoch:
                    raise ValueError("Source capsule consent epoch does not match tenant current epoch.")
                if capsule_row.lifecycle_state in ("TOMBSTONED", "PURGED", "QUARANTINED"):
                    raise ValueError(f"Source capsule in state '{capsule_row.lifecycle_state}' is not eligible.")

            # 4. Create Lesson in SOURCE_CHECKED state
            lesson = OperationalLesson(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                lesson_id=uuid.uuid4(),
                version=1,
                lesson_type=lesson_type,
                title=title.strip(),
                redacted_summary=sanitize_lesson_summary_for_llm(summary.strip()),
                applicability_predicate=pred,
                consent_epoch=current_epoch,
                confidence_band=confidence_band,
                lifecycle_state=LessonLifecycle.SOURCE_CHECKED.value,
                valid_from=current_time,
                valid_until=current_time + timedelta(days=valid_days),
                created_by=created_by,
                source_outcome_revision_id=source_outcome_revision_id,
                source_capsule_id=source_capsule_id,
                source_cluster_id=source_cluster_id,
                proposal_id=proposal_id,
                created_at=current_time,
                updated_at=current_time,
            )
            session.add(lesson)
            await session.commit()
            await session.refresh(lesson)
            return lesson

    async def validate_and_activate(
        self,
        tenant_id: uuid.UUID,
        lesson_id: uuid.UUID,
        version: int,
        reviewer_id: str,
        reviewer_role: str,
        *,
        now: datetime | None = None,
    ) -> OperationalLesson:
        """Authorized human reviewer validation and atomic activation of a lesson."""
        validate_reviewer_role(reviewer_role)
        current_time = now or datetime.now(UTC)

        async with self._session_factory() as session:
            stmt = select(OperationalLesson).where(
                OperationalLesson.tenant_id == tenant_id,
                OperationalLesson.lesson_id == lesson_id,
                OperationalLesson.version == version,
            ).with_for_update()
            lesson = (await session.execute(stmt)).scalar_one_or_none()
            if not lesson:
                raise LessonNotFoundError(f"Lesson '{lesson_id}' v{version} not found for tenant '{tenant_id}'.")

            # Check tenant consent epoch
            epoch_row = await session.get(ConsentEpoch, tenant_id)
            if not epoch_row or not epoch_row.reuse_allowed or epoch_row.current_epoch != lesson.consent_epoch:
                raise ValueError("Tenant consent epoch has changed or reuse is revoked.")

            # Check that outcome revision has not been superseded or disputed
            rev = await session.get(OutcomeRevision, lesson.source_outcome_revision_id)
            if not rev or rev.label in ("DISPUTED", "UNKNOWN"):
                lesson.lifecycle_state = LessonLifecycle.QUARANTINED.value
                lesson.quarantine_reason = "Source outcome was disputed or revoked before validation."
                await session.commit()
                raise ValueError("Cannot activate lesson: source outcome has been disputed.")

            newer_rev = (
                await session.execute(
                    select(OutcomeRevision.id).where(
                        OutcomeRevision.tenant_id == tenant_id,
                        OutcomeRevision.incident_id == rev.incident_id,
                        OutcomeRevision.revision_no > rev.revision_no,
                    )
                )
            ).scalar_one_or_none()
            if newer_rev:
                lesson.lifecycle_state = LessonLifecycle.QUARANTINED.value
                lesson.quarantine_reason = "Source outcome has been superseded by a newer revision."
                await session.commit()
                raise ValueError("Cannot activate lesson: source outcome was superseded.")

            lesson.lifecycle_state = LessonLifecycle.ACTIVE.value
            lesson.validated_by = reviewer_id
            lesson.validated_at = current_time
            lesson.updated_at = current_time

            await session.commit()
            await session.refresh(lesson)
            return lesson

    async def revoke_lesson(
        self,
        tenant_id: uuid.UUID,
        lesson_id: uuid.UUID,
        version: int,
        reason: str,
        actor_id: str,
        actor_role: str | None = None,
    ) -> OperationalLesson:
        """Revokes an active lesson immediately."""
        async with self._session_factory() as session:
            stmt = select(OperationalLesson).where(
                OperationalLesson.tenant_id == tenant_id,
                OperationalLesson.lesson_id == lesson_id,
                OperationalLesson.version == version,
            ).with_for_update()
            lesson = (await session.execute(stmt)).scalar_one_or_none()
            if not lesson:
                raise LessonNotFoundError(f"Lesson '{lesson_id}' v{version} not found for tenant '{tenant_id}'.")

            lesson.lifecycle_state = LessonLifecycle.REVOKED.value
            lesson.quarantine_reason = f"Revoked by {actor_id}: {reason}"
            lesson.updated_at = datetime.now(UTC)

            await session.commit()
            await session.refresh(lesson)
            return lesson
