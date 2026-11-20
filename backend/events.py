"""P1 tenant-scoped SSE notifications with bounded replay.

Notification data is advisory: clients must refetch the versioned snapshot.
Each tick owns a fresh database session; streaming never holds a transaction.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from uuid import UUID
from threading import Lock


class StreamCapacity:
    """Per-process stream cap; a durable cursor survives rejected reconnects."""

    def __init__(self, maximum: int = 32):
        if maximum < 1:
            raise ValueError("stream limit must be positive")
        self.maximum = maximum
        self.active = 0
        self._lock = Lock()

    def claim(self) -> bool:
        with self._lock:
            if self.active >= self.maximum:
                return False
            self.active += 1
            return True

    def release(self) -> None:
        with self._lock:
            if self.active < 1:
                raise RuntimeError("stream capacity over-released")
            self.active -= 1


STREAM_CAPACITY = StreamCapacity()


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
