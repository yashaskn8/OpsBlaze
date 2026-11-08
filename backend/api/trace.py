"""FastAPI routes for OpsBlaze Feature P3 — Evidence-to-Effect Trace Explorer."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.auth_deps import get_authenticated_tenant_id, get_session_factory
from backend.domain.incident import IncidentNotFoundError
from backend.domain.trace import TraceGraphResponse
from backend.trace.service import TraceService

router = APIRouter(prefix="/v1/incidents", tags=["trace"])


@router.get("/{incident_id}/trace", response_model=TraceGraphResponse)
async def get_incident_trace(
    incident_id: uuid.UUID,
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    tenant_id: Annotated[uuid.UUID, Depends(get_authenticated_tenant_id)],
) -> TraceGraphResponse:
    """Project server-side causally linked Directed Acyclic Graph (DAG) for an incident."""
    service = TraceService(session_factory)
    try:
        return await service.get_incident_trace_graph(tenant_id, incident_id)
    except IncidentNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Incident not found",
        ) from exc
