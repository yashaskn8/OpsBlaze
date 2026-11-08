"""FastAPI routes for OpsBlaze Feature E1 — Experience Capsule & Privacy Consent."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.domain.approval import ApprovalRoleForbiddenError
from backend.domain.experience import (
    CapsuleNotFoundError,
    CapsuleStateConflictError,
    ConsentDeniedError,
    ConsentStatusResponse,
    ConsentUpdateRequest,
    ExperienceCapsuleResponse,
    ExperienceError,
    ExperienceExportResponse,
    ExperienceListResponse,
    ExperienceQuarantineRequest,
)
from backend.experience.consent import ConsentManager
from backend.experience.repository import ExperienceRepository
from backend.persistence.models import Tenant

router = APIRouter(tags=["experience"])


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
        if tenant_exists is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Tenant {tenant_uuid} is not registered or access is denied",
            )
    return tenant_uuid


def get_experience_repository(
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> ExperienceRepository:
    return ExperienceRepository(session_factory)


def get_consent_manager(
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> ConsentManager:
    return ConsentManager(session_factory)


def _handle_experience_error(exc: Exception) -> HTTPException:
    if isinstance(exc, CapsuleNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, ConsentDeniedError | ApprovalRoleForbiddenError):
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    if isinstance(exc, CapsuleStateConflictError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, ExperienceError):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))


@router.get(
    "/v1/experiences",
    response_model=ExperienceListResponse,
    status_code=status.HTTP_200_OK,
)
async def list_experiences(
    tenant_id: Annotated[uuid.UUID, Depends(get_authenticated_tenant_id)],
    repo: Annotated[ExperienceRepository, Depends(get_experience_repository)],
    cursor: str | None = Query(None, description="Keyset pagination cursor"),
    limit: int = Query(20, ge=1, le=100, description="Items per page"),
) -> ExperienceListResponse:
    """List eligible experiences with query-time consent validation and keyset pagination."""
    try:
        return await repo.list_eligible_experiences(tenant_id, cursor=cursor, limit=limit)
    except Exception as exc:
        raise _handle_experience_error(exc) from exc


@router.post(
    "/v1/experiences/{capsule_id}/quarantine",
    response_model=ExperienceCapsuleResponse,
    status_code=status.HTTP_200_OK,
)
async def quarantine_experience(
    capsule_id: uuid.UUID,
    payload: ExperienceQuarantineRequest,
    tenant_id: Annotated[uuid.UUID, Depends(get_authenticated_tenant_id)],
    repo: Annotated[ExperienceRepository, Depends(get_experience_repository)],
) -> ExperienceCapsuleResponse:
    """Quarantine an experience capsule under human reviewer authority."""
    try:
        return await repo.quarantine_capsule(
            tenant_id=tenant_id,
            capsule_id=capsule_id,
            actor_id=payload.actor_id,
            actor_role=payload.actor_role,
            reason=payload.reason,
            expected_state=payload.expected_lifecycle_state,
        )
    except Exception as exc:
        raise _handle_experience_error(exc) from exc


@router.get(
    "/v1/privacy/experience-consent",
    response_model=ConsentStatusResponse,
    status_code=status.HTTP_200_OK,
)
async def get_consent_status(
    tenant_id: Annotated[uuid.UUID, Depends(get_authenticated_tenant_id)],
    consent_mgr: Annotated[ConsentManager, Depends(get_consent_manager)],
) -> ConsentStatusResponse:
    """Get current tenant consent status and epoch."""
    try:
        return await consent_mgr.get_consent(tenant_id)
    except Exception as exc:
        raise _handle_experience_error(exc) from exc


@router.post(
    "/v1/privacy/experience-consent",
    response_model=ConsentStatusResponse,
    status_code=status.HTTP_200_OK,
)
async def update_consent_status(
    payload: ConsentUpdateRequest,
    tenant_id: Annotated[uuid.UUID, Depends(get_authenticated_tenant_id)],
    consent_mgr: Annotated[ConsentManager, Depends(get_consent_manager)],
) -> ConsentStatusResponse:
    """Update or revoke tenant experience consent with atomic epoch advance."""
    try:
        if payload.reuse_allowed:
            return await consent_mgr.grant_consent(
                tenant_id=tenant_id,
                actor_id=payload.actor_id,
                actor_role=payload.actor_role,
                reason=payload.reason,
            )
        else:
            return await consent_mgr.revoke_consent(
                tenant_id=tenant_id,
                actor_id=payload.actor_id,
                actor_role=payload.actor_role,
                reason=payload.reason,
            )
    except Exception as exc:
        raise _handle_experience_error(exc) from exc


@router.get(
    "/v1/privacy/export",
    response_model=ExperienceExportResponse,
    status_code=status.HTTP_200_OK,
)
async def export_experiences(
    tenant_id: Annotated[uuid.UUID, Depends(get_authenticated_tenant_id)],
    repo: Annotated[ExperienceRepository, Depends(get_experience_repository)],
    max_records: int = Query(1000, ge=1, le=5000, description="Maximum capsules to export"),
) -> ExperienceExportResponse:
    """Export authorized redacted experience capsules for compliance and auditing."""
    try:
        return await repo.export_experiences(tenant_id, max_records=max_records)
    except Exception as exc:
        raise _handle_experience_error(exc) from exc
