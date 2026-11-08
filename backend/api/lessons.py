"""FastAPI routes for Feature E4 — Validated Operational Lesson Registry."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.domain.approval import ApprovalRoleForbiddenError, validate_reviewer_role
from backend.domain.lesson import LessonCitation, LessonType
from backend.learning.lesson_registry import LessonRegistry
from backend.learning.retrieval import LessonRetriever
from backend.persistence.models import OperationalLesson, Tenant

router = APIRouter(tags=["lessons"])


def get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database session factory is not initialized",
        )
    return factory  # type: ignore[no-any-return]


async def get_authenticated_tenant_id(
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    x_tenant_id: Annotated[str | None, Header(alias="X-Tenant-ID")] = None,
) -> uuid.UUID:
    """Derive and verify authenticated tenant identity from server-side headers."""
    if not x_tenant_id or not x_tenant_id.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing required X-Tenant-ID authentication header",
        )
    try:
        tenant_uuid = uuid.UUID(x_tenant_id.strip())
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Malformed X-Tenant-ID header: must be a valid UUID",
        )

    async with session_factory() as session:
        tenant_exists = await session.get(Tenant, tenant_uuid)
        if not tenant_exists:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Authenticated tenant {tenant_uuid} does not exist in authoritative store",
            )

    return tenant_uuid


# ---------------------------------------------------------------------------
# Request / Response Models
# ---------------------------------------------------------------------------


class DraftLessonRequest(BaseModel):
    title: str
    summary: str
    source_outcome_revision_id: uuid.UUID
    lesson_type: str = LessonType.VERIFIED_RECOVERY.value
    applicability_predicate: dict[str, Any] = {}
    source_capsule_id: uuid.UUID | None = None
    source_cluster_id: uuid.UUID | None = None
    proposal_id: uuid.UUID | None = None
    confidence_band: str = "MEDIUM"
    valid_days: int = 30
    created_by: str = "human_operator"


class ValidateLessonRequest(BaseModel):
    version: int
    reviewer_id: str
    reviewer_role: str


class RevokeLessonRequest(BaseModel):
    version: int
    reason: str
    actor_id: str


class SearchLessonRequest(BaseModel):
    query: str
    limit: int = 3
    max_tokens: int = 1500


class OperationalLessonResponse(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    lesson_id: uuid.UUID
    version: int
    lesson_type: str
    title: str
    redacted_summary: str
    applicability_predicate: dict[str, Any]
    consent_epoch: int
    confidence_band: str
    lifecycle_state: str
    valid_from: datetime
    valid_until: datetime
    created_by: str
    validated_by: str | None
    validated_at: datetime | None
    quarantine_reason: str | None
    source_outcome_revision_id: uuid.UUID
    source_capsule_id: uuid.UUID | None
    source_cluster_id: uuid.UUID | None
    proposal_id: uuid.UUID | None
    created_at: datetime


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/lessons", response_model=list[OperationalLessonResponse])
async def list_lessons(
    tenant_id: Annotated[uuid.UUID, Depends(get_authenticated_tenant_id)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[datetime | None, Query()] = None,
    lifecycle_state: Annotated[str | None, Query()] = None,
) -> list[OperationalLessonResponse]:
    """Retrieve keyset-paginated, tenant-isolated operational lessons."""
    async with session_factory() as session:
        query = select(OperationalLesson).where(OperationalLesson.tenant_id == tenant_id)
        if cursor:
            query = query.where(OperationalLesson.valid_from < cursor)
        if lifecycle_state:
            query = query.where(OperationalLesson.lifecycle_state == lifecycle_state)
        query = query.order_by(OperationalLesson.valid_from.desc()).limit(limit)

        lessons = (await session.execute(query)).scalars().all()
        return [
            OperationalLessonResponse(
                id=l.id,
                tenant_id=l.tenant_id,
                lesson_id=l.lesson_id,
                version=l.version,
                lesson_type=l.lesson_type,
                title=l.title,
                redacted_summary=l.redacted_summary,
                applicability_predicate=l.applicability_predicate,
                consent_epoch=l.consent_epoch,
                confidence_band=l.confidence_band,
                lifecycle_state=l.lifecycle_state,
                valid_from=l.valid_from,
                valid_until=l.valid_until,
                created_by=l.created_by,
                validated_by=l.validated_by,
                validated_at=l.validated_at,
                quarantine_reason=l.quarantine_reason,
                source_outcome_revision_id=l.source_outcome_revision_id,
                source_capsule_id=l.source_capsule_id,
                source_cluster_id=l.source_cluster_id,
                proposal_id=l.proposal_id,
                created_at=l.created_at,
            )
            for l in lessons
        ]


@router.post("/lessons", response_model=OperationalLessonResponse, status_code=status.HTTP_201_CREATED)
async def draft_lesson(
    request: DraftLessonRequest,
    tenant_id: Annotated[uuid.UUID, Depends(get_authenticated_tenant_id)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> OperationalLessonResponse:
    """Draft an operational lesson from attributable source outcomes and consent-checked capsules."""
    registry = LessonRegistry(session_factory)
    try:
        lesson = await registry.draft_lesson(
            tenant_id=tenant_id,
            title=request.title,
            summary=request.summary,
            source_outcome_revision_id=request.source_outcome_revision_id,
            lesson_type=request.lesson_type,
            applicability_predicate=request.applicability_predicate,
            source_capsule_id=request.source_capsule_id,
            source_cluster_id=request.source_cluster_id,
            proposal_id=request.proposal_id,
            confidence_band=request.confidence_band,
            valid_days=request.valid_days,
            created_by=request.created_by,
        )
        return OperationalLessonResponse(
            id=lesson.id,
            tenant_id=lesson.tenant_id,
            lesson_id=lesson.lesson_id,
            version=lesson.version,
            lesson_type=lesson.lesson_type,
            title=lesson.title,
            redacted_summary=lesson.redacted_summary,
            applicability_predicate=lesson.applicability_predicate,
            consent_epoch=lesson.consent_epoch,
            confidence_band=lesson.confidence_band,
            lifecycle_state=lesson.lifecycle_state,
            valid_from=lesson.valid_from,
            valid_until=lesson.valid_until,
            created_by=lesson.created_by,
            validated_by=lesson.validated_by,
            validated_at=lesson.validated_at,
            quarantine_reason=lesson.quarantine_reason,
            source_outcome_revision_id=lesson.source_outcome_revision_id,
            source_capsule_id=lesson.source_capsule_id,
            source_cluster_id=lesson.source_cluster_id,
            proposal_id=lesson.proposal_id,
            created_at=lesson.created_at,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.post("/lessons/{lesson_id}/validate", response_model=OperationalLessonResponse)
async def validate_lesson(
    lesson_id: uuid.UUID,
    request: ValidateLessonRequest,
    tenant_id: Annotated[uuid.UUID, Depends(get_authenticated_tenant_id)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> OperationalLessonResponse:
    """Authorized human reviewer validation and activation of a lesson."""
    try:
        validate_reviewer_role(request.reviewer_role)
    except ApprovalRoleForbiddenError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))

    registry = LessonRegistry(session_factory)
    try:
        lesson = await registry.validate_and_activate(
            tenant_id=tenant_id,
            lesson_id=lesson_id,
            version=request.version,
            reviewer_id=request.reviewer_id,
            reviewer_role=request.reviewer_role,
        )
        return OperationalLessonResponse(
            id=lesson.id,
            tenant_id=lesson.tenant_id,
            lesson_id=lesson.lesson_id,
            version=lesson.version,
            lesson_type=lesson.lesson_type,
            title=lesson.title,
            redacted_summary=lesson.redacted_summary,
            applicability_predicate=lesson.applicability_predicate,
            consent_epoch=lesson.consent_epoch,
            confidence_band=lesson.confidence_band,
            lifecycle_state=lesson.lifecycle_state,
            valid_from=lesson.valid_from,
            valid_until=lesson.valid_until,
            created_by=lesson.created_by,
            validated_by=lesson.validated_by,
            validated_at=lesson.validated_at,
            quarantine_reason=lesson.quarantine_reason,
            source_outcome_revision_id=lesson.source_outcome_revision_id,
            source_capsule_id=lesson.source_capsule_id,
            source_cluster_id=lesson.source_cluster_id,
            proposal_id=lesson.proposal_id,
            created_at=lesson.created_at,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.post("/lessons/{lesson_id}/revoke", response_model=OperationalLessonResponse)
async def revoke_lesson(
    lesson_id: uuid.UUID,
    request: RevokeLessonRequest,
    tenant_id: Annotated[uuid.UUID, Depends(get_authenticated_tenant_id)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> OperationalLessonResponse:
    """Revokes an active lesson with explicit reason tracking."""
    registry = LessonRegistry(session_factory)
    try:
        lesson = await registry.revoke_lesson(
            tenant_id=tenant_id,
            lesson_id=lesson_id,
            version=request.version,
            reason=request.reason,
            actor_id=request.actor_id,
        )
        return OperationalLessonResponse(
            id=lesson.id,
            tenant_id=lesson.tenant_id,
            lesson_id=lesson.lesson_id,
            version=lesson.version,
            lesson_type=lesson.lesson_type,
            title=lesson.title,
            redacted_summary=lesson.redacted_summary,
            applicability_predicate=lesson.applicability_predicate,
            consent_epoch=lesson.consent_epoch,
            confidence_band=lesson.confidence_band,
            lifecycle_state=lesson.lifecycle_state,
            valid_from=lesson.valid_from,
            valid_until=lesson.valid_until,
            created_by=lesson.created_by,
            validated_by=lesson.validated_by,
            validated_at=lesson.validated_at,
            quarantine_reason=lesson.quarantine_reason,
            source_outcome_revision_id=lesson.source_outcome_revision_id,
            source_capsule_id=lesson.source_capsule_id,
            source_cluster_id=lesson.source_cluster_id,
            proposal_id=lesson.proposal_id,
            created_at=lesson.created_at,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.post("/lessons/search", response_model=list[LessonCitation])
async def search_lessons(
    request: SearchLessonRequest,
    tenant_id: Annotated[uuid.UUID, Depends(get_authenticated_tenant_id)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> list[LessonCitation]:
    """Search lessons via PostgreSQL native full-text search with synchronous query-time invalidation."""
    retriever = LessonRetriever(session_factory)
    return await retriever.retrieve_lessons(
        tenant_id=tenant_id,
        query_text=request.query,
        limit=request.limit,
        max_tokens=request.max_tokens,
    )
