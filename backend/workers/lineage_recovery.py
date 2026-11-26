"""Repair the lineage cache one committed page at a time."""

import asyncio
from datetime import datetime, timedelta, timezone
import logging
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.lineage_projection import SOURCE_MODELS, rebuild_evidence_page, scan_orphan_edges
from backend.models import Incident, LineageRecoveryCheckpoint

PHASES = (*SOURCE_MODELS, "orphans")
INTERVAL = timedelta(minutes=1)
log = logging.getLogger(__name__)


async def recover_lineage_page(
    sessions: async_sessionmaker[AsyncSession], *, limit: int = 20,
    now: datetime | None = None,
) -> bool:
    """Advance one page, committing the repair and its checkpoint together."""
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError("invalid lineage recovery page size")
    now = now or datetime.now(timezone.utc)
    async with sessions.begin() as session:
        if session.bind.dialect.name == "postgresql":
            await session.execute(text("SET LOCAL lock_timeout = '1s'"))
            await session.execute(text("SET LOCAL statement_timeout = '3s'"))
        checkpoint = await session.scalar(select(LineageRecoveryCheckpoint).where(
            LineageRecoveryCheckpoint.id == 1,
        ).with_for_update(skip_locked=True))
        if checkpoint is None:
            return False

        if checkpoint.phase is None:
            completed_at = checkpoint.completed_at
            if checkpoint.incident_id is None and completed_at is not None:
                # PG returns an aware timestamp. SQLite stores the same instant without a zone.
                if completed_at.tzinfo is None:
                    completed_at = completed_at.replace(tzinfo=timezone.utc)
                if now < completed_at + INTERVAL:
                    return False
            query = select(Incident.id, Incident.tenant_id).order_by(Incident.id).limit(1)
            if checkpoint.incident_id is not None:
                query = query.where(Incident.id > checkpoint.incident_id)
            incident = (await session.execute(query)).one_or_none()
            if incident is None:
                checkpoint.incident_id = None
                checkpoint.completed_at = now
                return False
            checkpoint.incident_id = incident.id
            checkpoint.phase = PHASES[0]
            tenant = incident.tenant_id
        else:
            tenant = await session.scalar(select(Incident.tenant_id).where(
                Incident.id == checkpoint.incident_id,
            ))
            if tenant is None:
                checkpoint.phase = checkpoint.source_cursor = checkpoint.edge_cursor = None
                return True

        if checkpoint.phase == "orphans":
            _, checkpoint.edge_cursor = await scan_orphan_edges(
                session, tenant, checkpoint.incident_id, checkpoint.edge_cursor, limit,
            )
            if checkpoint.edge_cursor is None:
                checkpoint.phase = None
        else:
            page = await rebuild_evidence_page(
                session, tenant, checkpoint.incident_id, checkpoint.phase,
                cursor=checkpoint.source_cursor, limit=limit,
            )
            if page is None:
                checkpoint.phase = checkpoint.source_cursor = None
            elif page["next_cursor"] is None:
                checkpoint.phase = PHASES[PHASES.index(checkpoint.phase) + 1]
                checkpoint.source_cursor = None
            else:
                checkpoint.source_cursor = UUID(page["next_cursor"])
        return True


async def lineage_recovery_loop(sessions: async_sessionmaker[AsyncSession]) -> None:
    while True:
        try:
            advanced = await recover_lineage_page(sessions)
        except SQLAlchemyError as exc:
            log.warning("Lineage recovery page deferred: class=%s", type(exc).__name__)
            advanced = False
        await asyncio.sleep(0.25 if advanced else 5)
