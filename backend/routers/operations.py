"""Operational readiness and bounded operator maintenance HTTP routes."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth import local_operator, local_security_admin, local_tenant
from backend.database import get_session
from backend.settings import Settings, get_settings

router = APIRouter()


@router.get("/v1/health/ready")
async def readiness(
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    from sqlalchemy import text
    from backend.workers.heartbeat import (
        MULTIHOST_ROLES, REQUIRED_ROLES, present_worker_host_counts, present_workers,
    )

    from backend.workers.temporal_health import check_temporal_health
    from backend.temporal_connection import validate_temporal_transport
    from backend.auth import configured_identity

    if settings.environment == "production":
        if not configured_identity(settings):
            raise HTTPException(503, "production identity provider is not configured")
        from backend.workers.experience_projector import (
            validate_projector_identity_reference,
        )

        try:
            # The API must not hold the projector database password. A
            # separate worker validates its scoped login and privileges.
            # Readiness also requires that worker's fresh heartbeat.
            validate_projector_identity_reference(settings)
        except ValueError as exc:
            raise HTTPException(
                503, "dedicated experience projector credentials not configured",
            ) from exc

    try:
        await session.execute(text("SELECT 1"))
        if settings.environment == "production":
            present = await present_workers(
                session,
                max_age_seconds=settings.worker_stale_seconds,
            )
            if not REQUIRED_ROLES.issubset(present):
                raise HTTPException(503, "required worker heartbeat missing or stale")
            if settings.min_distinct_worker_hosts > 1:
                host_counts = await present_worker_host_counts(
                    session,
                    max_age_seconds=settings.worker_stale_seconds,
                )
                if any(
                    host_counts[role] < settings.min_distinct_worker_hosts
                    for role in MULTIHOST_ROLES
                ):
                    raise HTTPException(
                        503, "configured multi-host worker presence not observed",
                    )
        # Never hold an active PostgreSQL read transaction over an RPC.
        await session.rollback()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, "database unavailable") from exc

    if settings.environment == "production":
        try:
            validate_temporal_transport(settings)
            if not await check_temporal_health(settings):
                raise HTTPException(503, "Temporal health check failed")
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(503, "Temporal unreachable") from exc
    return {"status": "ready"}

@router.get("/v1/operations/queues")
async def tenant_queues(
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, int]:
    from backend.operations import queue_health

    return await queue_health(
        session, tenant_id, settings.stuck_after_seconds,
    )


@router.get("/v1/operations/dead-letters")
async def experience_dead_letters(
    cursor: int = Query(default=0, ge=0, le=(1 << 63) - 1),
    limit: int = Query(default=50, ge=1, le=50),
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from backend.workers.dead_letters import list_projection_dead_letters

    return await list_projection_dead_letters(session, tenant_id, cursor, limit)


@router.post("/v1/operations/dead-letters/{entry_id}/acknowledge", status_code=202)
async def acknowledge_experience_dead_letter(
    entry_id: int,
    if_match: Annotated[str, Header()],
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
    operator_identity: str = Depends(local_operator),
) -> dict:
    from backend.workers.dead_letters import (
        DeadLetterConflict, acknowledge_projection_dead_letter,
    )

    if entry_id < 1 or entry_id > (1 << 63) - 1:
        raise HTTPException(422, "invalid dead-letter identifier")
    version = if_match.strip('"')
    if not version.isdecimal() or not 0 < len(version) <= 10:
        raise HTTPException(400, "If-Match must be the delivery attempt count")
    try:
        row = await acknowledge_projection_dead_letter(
            session, tenant_id, entry_id, int(version), operator_identity,
        )
    except DeadLetterConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    if row is None:
        raise HTTPException(404, "experience dead letter not found")
    return {"id": row.id, "reviewed": True, "attempts": row.attempt}


class ProjectionRedriveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: Literal["STORE_RESTORED", "TRANSIENT_FIX_VERIFIED"]


@router.post("/v1/operations/dead-letters/{entry_id}/redrive", status_code=202)
async def redrive_experience_dead_letter(
    entry_id: int,
    request: ProjectionRedriveRequest,
    if_match: Annotated[str, Header()],
    tenant_id: str = Depends(local_tenant),
    session: AsyncSession = Depends(get_session),
    security_admin: str = Depends(local_security_admin),
) -> dict:
    from sqlalchemy.exc import IntegrityError
    from backend.workers.dead_letters import (
        DeadLetterConflict, redrive_projection_dead_letter,
    )

    if not 1 <= entry_id <= (1 << 63) - 1:
        raise HTTPException(422, "invalid dead-letter identifier")
    version = if_match.strip('"')
    if not version.isdecimal() or not 0 < len(version) <= 10:
        raise HTTPException(400, "If-Match must be the delivery attempt count")
    try:
        row = await redrive_projection_dead_letter(
            session, tenant_id, entry_id, int(version),
            security_admin, request.reason,
        )
    except DeadLetterConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except IntegrityError as exc:
        # Concurrent first-writer claim resolves via a unique source index.
        raise HTTPException(409, "projection already redriven") from exc
    if row is None:
        raise HTTPException(404, "experience dead letter not found")
    return {
        "id": row.id, "source_id": entry_id,
        "state": "PENDING_PROJECTION",
    }
