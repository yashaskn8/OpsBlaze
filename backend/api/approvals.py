"""FastAPI routes for OpsBlaze Feature P2 — Version-Bound Approval Cockpit."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.auth_deps import (
    get_authenticated_principal,
    get_session_factory,
)
from backend.approvals.service import ApprovalService
from backend.domain.approval import (
    ALLOWED_APPROVAL_ROLES,
    ApprovalCreateRequest,
    ApprovalDecisionRequest,
    ApprovalDigestMismatchError,
    ApprovalError,
    ApprovalExpiredError,
    ApprovalNotFoundError,
    ApprovalPolicyReleaseForbiddenError,
    ApprovalResponse,
    ApprovalRevokedError,
    ApprovalRevokeRequest,
    ApprovalRoleForbiddenError,
    ApprovalVersionMismatchError,
    InvalidApprovalTransitionError,
    StaleContextError,
)
from backend.domain.auth import AuthorizationForbiddenError, Principal
from backend.domain.incident import IncidentNotFoundError

router = APIRouter(tags=["approvals"])


def _handle_approval_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ApprovalNotFoundError | IncidentNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, ApprovalRoleForbiddenError | ApprovalPolicyReleaseForbiddenError | AuthorizationForbiddenError):
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    if isinstance(exc, StaleContextError | InvalidApprovalTransitionError | ApprovalRevokedError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, IntegrityError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Conflict: active approval already exists or unique constraint violated",
        )
    if isinstance(exc, ApprovalExpiredError):
        return HTTPException(status_code=status.HTTP_410_GONE, detail=str(exc))
    if isinstance(exc, ApprovalDigestMismatchError | ApprovalVersionMismatchError):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        )
    if isinstance(exc, ApprovalError):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))


def _assert_approval_authority(principal: Principal) -> str:
    """Verifies that calling principal is a human with authorized approval role, returning canonical role."""
    principal.require_human()
    matching_roles = [r for r in sorted(principal.roles) if r in ALLOWED_APPROVAL_ROLES]
    if not matching_roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Principal '{principal.actor_id}' lacks required approval authority. "
                f"Required one of: {sorted(ALLOWED_APPROVAL_ROLES)}; principal roles: {sorted(principal.roles)}"
            ),
        )
    return matching_roles[0]


@router.post("/v1/plans/{plan_id}/approvals", response_model=ApprovalResponse, status_code=status.HTTP_201_CREATED)
async def create_plan_approval(
    plan_id: str,
    payload: ApprovalCreateRequest,
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    principal: Annotated[Principal, Depends(get_authenticated_principal)],
    incident_id: Annotated[uuid.UUID, Query(description="Incident ID to bind this approval to")],
) -> ApprovalResponse:
    """Create a version-bound pending approval request for a proposed plan."""
    if payload.plan_id != plan_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Plan ID in path ('{plan_id}') does not match payload ('{payload.plan_id}')",
        )
    service = ApprovalService(session_factory)
    try:
        return await service.create_approval_request(principal.tenant_id, incident_id, payload)
    except Exception as exc:
        raise _handle_approval_error(exc) from exc


@router.get("/v1/approvals/{approval_id}", response_model=ApprovalResponse)
async def get_approval_by_id(
    approval_id: uuid.UUID,
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    principal: Annotated[Principal, Depends(get_authenticated_principal)],
) -> ApprovalResponse:
    """Fetch approval state, reviewer, expiration, and binding metadata."""
    service = ApprovalService(session_factory)
    try:
        return await service.get_approval(principal.tenant_id, approval_id)
    except Exception as exc:
        raise _handle_approval_error(exc) from exc


@router.post("/v1/approvals/{approval_id}/approve", response_model=ApprovalResponse)
async def approve_plan(
    approval_id: uuid.UUID,
    payload: ApprovalDecisionRequest,
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    principal: Annotated[Principal, Depends(get_authenticated_principal)],
) -> ApprovalResponse:
    """Submit human reviewer approval for a proposed incident plan. Reviewer identity is server-derived."""
    canonical_role = _assert_approval_authority(principal)
    server_decision = ApprovalDecisionRequest(
        reviewer_id=principal.actor_id,
        reviewer_role=canonical_role,
        reason=payload.reason,
        expected_incident_status_version=payload.expected_incident_status_version,
    )
    service = ApprovalService(session_factory)
    try:
        return await service.approve(principal.tenant_id, approval_id, server_decision)
    except Exception as exc:
        raise _handle_approval_error(exc) from exc


@router.post("/v1/approvals/{approval_id}/reject", response_model=ApprovalResponse)
async def reject_plan(
    approval_id: uuid.UUID,
    payload: ApprovalDecisionRequest,
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    principal: Annotated[Principal, Depends(get_authenticated_principal)],
) -> ApprovalResponse:
    """Submit human reviewer rejection for a proposed incident plan. Reviewer identity is server-derived."""
    canonical_role = _assert_approval_authority(principal)
    server_decision = ApprovalDecisionRequest(
        reviewer_id=principal.actor_id,
        reviewer_role=canonical_role,
        reason=payload.reason,
        expected_incident_status_version=payload.expected_incident_status_version,
    )
    service = ApprovalService(session_factory)
    try:
        return await service.reject(principal.tenant_id, approval_id, server_decision)
    except Exception as exc:
        raise _handle_approval_error(exc) from exc


@router.post("/v1/approvals/{approval_id}/revoke", response_model=ApprovalResponse)
async def revoke_plan_approval(
    approval_id: uuid.UUID,
    payload: ApprovalRevokeRequest,
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    principal: Annotated[Principal, Depends(get_authenticated_principal)],
) -> ApprovalResponse:
    """Revoke an approved plan decision before dispatch. Reviewer identity is server-derived."""
    canonical_role = _assert_approval_authority(principal)
    server_decision = ApprovalRevokeRequest(
        reviewer_id=principal.actor_id,
        reviewer_role=canonical_role,
        reason=payload.reason,
    )
    service = ApprovalService(session_factory)
    try:
        return await service.revoke(principal.tenant_id, approval_id, server_decision)
    except Exception as exc:
        raise _handle_approval_error(exc) from exc
