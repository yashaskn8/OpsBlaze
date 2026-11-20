"""B4 read-only monitor: aged active phases and undelivered outbox intents."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from backend.database import get_session_maker
from backend.models import Incident, OutboxEntry
from backend.services import ACTIVE_STATES
from backend.settings import get_settings

log = logging.getLogger(__name__)


async def scan_stuck() -> int:
    threshold = datetime.now(timezone.utc) - timedelta(
        seconds=get_settings().stuck_after_seconds
    )
    async with get_session_maker()() as session:
        incidents = (await session.scalars(
            select(Incident).where(
                Incident.phase_entered_at < threshold,
                Incident.status_projection.in_(ACTIVE_STATES),
            )
            .order_by(Incident.phase_entered_at)
            .limit(100)
        )).all()
        intents = (await session.scalars(
            select(OutboxEntry).where(
                OutboxEntry.created_at < threshold,
                OutboxEntry.delivered_at.is_(None),
            )
            .order_by(OutboxEntry.created_at)
            .limit(100)
        )).all()
    for incident in incidents:
        log.warning(
            "Incident exceeded phase-age threshold: incident_id=%s phase=%s",
            incident.id, incident.status_projection,
        )
    for intent in intents:
        log.warning(
            "Outbox delivery exceeded age threshold: intent_id=%s kind=%s attempt=%s",
            intent.id, intent.kind, intent.attempt,
        )
    return len(incidents) + len(intents)


async def main() -> None:
    while True:
        await scan_stuck()
        await asyncio.sleep(30)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
