"""FastAPI routes for OpsBlaze Feature P1 — Live Investigation Workbench & SSE."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.auth_deps import get_authenticated_tenant_id, get_session_factory
from backend.domain.incident import IncidentNotFoundError
from backend.domain.workbench import WorkbenchSnapshotResponse
from backend.persistence.repositories import IncidentRepository
from backend.workbench.service import WorkbenchService

logger = logging.getLogger("opsblaze.workbench_api")

router = APIRouter(prefix="/v1/incidents", tags=["workbench"])


@router.get("/{incident_id}/workbench", response_model=WorkbenchSnapshotResponse)
async def get_workbench_snapshot(
    incident_id: uuid.UUID,
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    tenant_id: Annotated[uuid.UUID, Depends(get_authenticated_tenant_id)],
    response: Response,
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
) -> Any:
    """Fetch live investigation workbench state with ETag caching and 304 validation."""
    service = WorkbenchService(session_factory)
    try:
        snapshot, etag = await service.get_workbench_snapshot(tenant_id, incident_id)
    except IncidentNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Incident not found",
        ) from exc

    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "no-cache, must-revalidate"

    if if_none_match and if_none_match.strip() == etag:
        return Response(
            status_code=status.HTTP_304_NOT_MODIFIED,
            headers={"ETag": etag, "Cache-Control": "no-cache, must-revalidate"},
        )

    return snapshot


@router.get("/{incident_id}/events")
async def stream_incident_events(
    incident_id: uuid.UUID,
    request: Request,
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    tenant_id: Annotated[uuid.UUID, Depends(get_authenticated_tenant_id)],
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ping_interval_seconds: Annotated[float, Query(description="Heartbeat ping interval in seconds")] = 10.0,
    max_events: Annotated[int | None, Query(description="Optional limit on event emissions for testing")] = None,
) -> StreamingResponse:
    """Server-Sent Events (SSE) notification stream with monotonic event IDs and heartbeats."""
    repo = IncidentRepository(session_factory)
    inc = await repo.get_incident(tenant_id, incident_id)
    if inc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Incident not found",
        )

    async def event_generator() -> AsyncIterator[str]:
        seq = 0
        if last_event_id and last_event_id.isdigit():
            seq = int(last_event_id)

        events_sent = 0
        last_ping = asyncio.get_running_loop().time()
        last_observed_status_version = inc.status_version
        last_observed_status = inc.status.value

        # Initial connect event
        seq += 1
        events_sent += 1
        init_payload = {
            "incident_id": str(incident_id),
            "status": last_observed_status,
            "status_version": last_observed_status_version,
            "connected_at": datetime.now(UTC).isoformat(),
        }
        yield f"id: {seq}\nevent: connected\ndata: {json.dumps(init_payload)}\n\n"

        try:
            while True:
                if max_events is not None and events_sent >= max_events:
                    break

                if await request.is_disconnected():
                    break

                await asyncio.sleep(0.5)

                now_loop = asyncio.get_running_loop().time()

                # 1. Check for incident updates
                try:
                    current_snap = await repo.get_incident(tenant_id, incident_id)
                    if current_snap is None:
                        break
                    if (
                        current_snap.status_version != last_observed_status_version
                        or current_snap.status.value != last_observed_status
                    ):
                        seq += 1
                        events_sent += 1
                        last_observed_status_version = current_snap.status_version
                        last_observed_status = current_snap.status.value
                        update_payload = {
                            "incident_id": str(incident_id),
                            "status": current_snap.status.value,
                            "status_version": current_snap.status_version,
                            "timestamp": datetime.now(UTC).isoformat(),
                        }
                        yield f"id: {seq}\nevent: incident_updated\ndata: {json.dumps(update_payload)}\n\n"
                        if max_events is not None and events_sent >= max_events:
                            break
                except (SQLAlchemyError, OSError, RuntimeError) as exc:
                    logger.debug("Transient error polling incident update: %s", exc)

                # 2. Check for heartbeat ping interval
                if now_loop - last_ping >= ping_interval_seconds:
                    seq += 1
                    events_sent += 1
                    last_ping = now_loop
                    ping_payload = {
                        "server_time": datetime.now(UTC).isoformat(),
                        "incident_id": str(incident_id),
                    }
                    yield f"id: {seq}\nevent: ping\ndata: {json.dumps(ping_payload)}\n\n"
                    if max_events is not None and events_sent >= max_events:
                        break

        except asyncio.CancelledError:
            # Client disconnected
            pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
