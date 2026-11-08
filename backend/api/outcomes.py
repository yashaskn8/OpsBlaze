"""FastAPI routes for OpsBlaze Feature E2 — Independent Outcome Adjudication Ledger."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.domain.approval import ApprovalRoleForbiddenError
from backend.domain.incident import IncidentNotFoundError
from backend.domain.outcome import (
    ObservationCreateRequest,
    OperatorFeedbackRequest,
    OutcomeConflictError,
    OutcomeError,
    OutcomeNotFoundError,
    OutcomeRevisionResponse,
    OutcomeSummaryResponse,
    SelfGradingForbiddenError,
)
from backend.outcomes.service import OutcomeService
from backend.persistence.models import Tenant

router = APIRouter(tags=["outcomes"])


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


def get_outcome_service(
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> OutcomeService:
    return OutcomeService(session_factory)


def _handle_outcome_error(exc: Exception) -> HTTPException:
    if isinstance(exc, IncidentNotFoundError | OutcomeNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, SelfGradingForbiddenError | ApprovalRoleForbiddenError):
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    if isinstance(exc, OutcomeConflictError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, OutcomeError):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))


@router.get(
    "/v1/incidents/{incident_id}/outcome",
    response_model=OutcomeSummaryResponse,
    status_code=status.HTTP_200_OK,
)
async def get_incident_outcome(
    incident_id: uuid.UUID,
    tenant_id: Annotated[uuid.UUID, Depends(get_authenticated_tenant_id)],
    service: Annotated[OutcomeService, Depends(get_outcome_service)],
) -> OutcomeSummaryResponse:
    """Get the current outcome status, latest revision, and all observations for an incident."""
    try:
        return await service.get_outcome_summary(tenant_id, incident_id)
    except Exception as exc:
        raise _handle_outcome_error(exc) from exc


@router.post(
    "/v1/incidents/{incident_id}/outcome/observations",
    response_model=dict[str, Any],
    status_code=status.HTTP_201_CREATED,
)
async def record_observation(
    incident_id: uuid.UUID,
    payload: ObservationCreateRequest,
    tenant_id: Annotated[uuid.UUID, Depends(get_authenticated_tenant_id)],
    service: Annotated[OutcomeService, Depends(get_outcome_service)],
) -> dict[str, Any]:
    """Record an append-only observation and trigger outcome rule adjudication."""
    try:
        obs, rev = await service.record_observation(tenant_id, incident_id, payload)
        return {
            "observation": obs.model_dump(mode="json"),
            "current_revision": rev.model_dump(mode="json"),
        }
    except Exception as exc:
        raise _handle_outcome_error(exc) from exc


@router.post(
    "/v1/incidents/{incident_id}/outcome/feedback",
    response_model=OutcomeRevisionResponse,
    status_code=status.HTTP_200_OK,
)
async def submit_operator_feedback(
    incident_id: uuid.UUID,
    payload: OperatorFeedbackRequest,
    tenant_id: Annotated[uuid.UUID, Depends(get_authenticated_tenant_id)],
    service: Annotated[OutcomeService, Depends(get_outcome_service)],
) -> OutcomeRevisionResponse:
    """Submit human reviewer feedback or resolve a dispute using optimistic revision locking."""
    try:
        return await service.submit_operator_feedback(tenant_id, incident_id, payload)
    except Exception as exc:
        raise _handle_outcome_error(exc) from exc
