"""FastAPI routes for OpsBlaze Feature B1 — Durable Incident Workflow."""

from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client
from temporalio.exceptions import TemporalError

from backend.domain.incident import (
    CANCELLABLE_STATUSES,
    IdempotencyConflictError,
    IncidentCancelRequest,
    IncidentCreateRequest,
    IncidentNotFoundError,
    IncidentResponse,
    IncidentStatus,
    InvalidStateTransitionError,
    StaleStateVersionError,
    WorkflowIncidentInput,
    canonical_request_hash,
    validate_client_key,
)
from backend.investigator.fake import FakeInvestigator
from backend.observability.metrics import get_correlation_id
from backend.persistence.repositories import IncidentRepository
from backend.releases.router_service import PolicyRouterService

router = APIRouter(prefix="/v1/incidents", tags=["incidents"])
logger = logging.getLogger("opsblaze.api.incidents")

_fake_investigator = FakeInvestigator()


def get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database session factory is not initialized",
        )
    return factory  # type: ignore[no-any-return]


from backend.api.auth_deps import get_authenticated_tenant_id


def get_idempotency_key(
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> str:
    if idempotency_key is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Missing required Idempotency-Key header",
        )
    try:
        return validate_client_key(idempotency_key)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc


def _attach_investigation_if_applicable(resp: IncidentResponse) -> IncidentResponse:
    if resp.status in {
        IncidentStatus.AWAITING_APPROVAL,
        IncidentStatus.EXECUTING,
        IncidentStatus.VERIFYING,
        IncidentStatus.VERIFIED,
        IncidentStatus.NEEDS_REVIEW,
    }:
        inv = _fake_investigator.investigate(
            WorkflowIncidentInput(
                tenant_id=str(resp.tenant_id),
                incident_id=str(resp.id),
                target_id=resp.target_id,
                request_sha256=resp.request_sha256,
            )
        )
        return resp.model_copy(update={"investigation": inv})
    return resp


@router.post(
    "",
    response_model=IncidentResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_incident(
    payload: IncidentCreateRequest,
    tenant_id: Annotated[uuid.UUID, Depends(get_authenticated_tenant_id)],
    client_key: Annotated[str, Depends(get_idempotency_key)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> IncidentResponse:
    repo = IncidentRepository(session_factory)
    req_hash = canonical_request_hash(payload.to_canonical_payload())
    cid = get_correlation_id()

    try:
        outcome = await repo.admit_incident(
            tenant_id=tenant_id,
            client_key=client_key,
            target_id=payload.target_id,
            request_sha256=req_hash,
            correlation_id=cid,
        )
    except IdempotencyConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Database constraint conflict during incident admission",
        ) from exc
    except (OperationalError, SQLAlchemyError, OSError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database unavailable during incident admission",
        ) from exc

    if outcome.created:
        # Route only newly admitted incidents. Shadow assignments stay on the incumbent arm;
        # a durable shadow outbox event is created by PolicyRouterService in the same DB commit.
        try:
            await PolicyRouterService(session_factory).route_incident(
                tenant_id=tenant_id,
                incident_id=outcome.response.id,
                target_id=outcome.response.target_id,
                target_component="investigator_planner",
            )
        except (RuntimeError, ValueError, SQLAlchemyError) as routing_error:
            # No active release/incumbent bundle is a valid no-rollout state. Incident admission
            # remains authoritative and must not be rolled back by optional release routing.
            logger.warning(
                "Policy routing skipped for incident %s: %s",
                outcome.response.id,
                type(routing_error).__name__,
            )

    return _attach_investigation_if_applicable(outcome.response)


@router.get(
    "/{incident_id}",
    response_model=IncidentResponse,
    status_code=status.HTTP_200_OK,
)
async def get_incident(
    incident_id: uuid.UUID,
    tenant_id: Annotated[uuid.UUID, Depends(get_authenticated_tenant_id)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> IncidentResponse:
    repo = IncidentRepository(session_factory)
    try:
        resp = await repo.get_incident(tenant_id=tenant_id, incident_id=incident_id)
    except IncidentNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Incident not found",
        ) from exc
    except (OperationalError, SQLAlchemyError, OSError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database unavailable while fetching incident",
        ) from exc

    return _attach_investigation_if_applicable(resp)


@router.post(
    "/{incident_id}/cancel",
    response_model=IncidentResponse,
    status_code=status.HTTP_200_OK,
)
async def cancel_incident(
    incident_id: uuid.UUID,
    request: Request,
    tenant_id: Annotated[uuid.UUID, Depends(get_authenticated_tenant_id)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    cancel_body: IncidentCancelRequest | None = None,
) -> IncidentResponse:
    repo = IncidentRepository(session_factory)
    body = cancel_body or IncidentCancelRequest()
    cid = get_correlation_id()

    try:
        current = await repo.get_incident(tenant_id=tenant_id, incident_id=incident_id)
        if (
            current.status != IncidentStatus.CANCELLED
            and current.status not in CANCELLABLE_STATUSES
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Cannot cancel incident in state {current.status}",
            )

        updated = await repo.transition_status(
            tenant_id=tenant_id,
            incident_id=incident_id,
            target_status=IncidentStatus.CANCELLED,
            expected_version=body.expected_version,
            correlation_id=cid,
        )
    except IncidentNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Incident not found",
        ) from exc
    except (InvalidStateTransitionError, StaleStateVersionError) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except (OperationalError, SQLAlchemyError, OSError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database unavailable during incident cancellation",
        ) from exc

    # Best-effort signal to Temporal workflow outside the DB transaction if a client is attached
    temporal_client: Client | None = getattr(request.app.state, "temporal_client", None)
    if temporal_client is not None:
        try:
            handle = temporal_client.get_workflow_handle(updated.workflow_id)
            await handle.signal("request_cancel", body.reason or "api_cancel")
        except (TemporalError, RuntimeError, OSError):
            # Workflow may not have been started yet or may already be completed;
            # PostgreSQL cancellation and outbox suppression remain authoritative.
            pass

    return _attach_investigation_if_applicable(updated)
