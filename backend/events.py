"""P1 tenant-scoped SSE notifications with bounded replay.

Notification data is advisory: clients must refetch the versioned snapshot.
Each tick owns a fresh database session; streaming never holds a transaction.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.models import UIEvent


async def read_event_batch(
    sessions: async_sessionmaker[AsyncSession],
    tenant_id: str,
    incident_id: UUID,
    after_id: int,
) -> list[UIEvent]:
    async with sessions() as session:
        return list((await session.scalars(
            select(UIEvent)
            .where(
                UIEvent.tenant_id == tenant_id,
                UIEvent.incident_id == incident_id,
                UIEvent.id > after_id,
            )
            .order_by(UIEvent.id)
            .limit(100)
        )).all())


def encode_sse(event: UIEvent) -> str:
    body = json.dumps(
        {
            "id": event.id,
            "kind": event.kind,
            "projection_version": event.projection_version,
            "created_at": event.created_at.isoformat(),
        },
        separators=(",", ":"),
    )
    return f"id: {event.id}\nevent: incident\ndata: {body}\n\n"


async def stream_incident_events(
    sessions: async_sessionmaker[AsyncSession],
    tenant_id: str,
    incident_id: UUID,
    after_id: int,
    disconnected: Callable[[], Awaitable[bool]],
    poll_seconds: float = 2.0,
    max_ticks: int = 30,
) -> AsyncIterator[str]:
    """Bound each stream lifetime so a reconnect can recover missed events."""
    for _ in range(max_ticks):
        if await disconnected():
            return
        rows = await read_event_batch(sessions, tenant_id, incident_id, after_id)
        if rows:
            for event in rows:
                if await disconnected():
                    return
                after_id = event.id
                yield encode_sse(event)
        else:
            yield ": heartbeat\n\n"
        await asyncio.sleep(poll_seconds)
