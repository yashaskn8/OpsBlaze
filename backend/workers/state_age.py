"""Read-only monitor for aged incident phases; never silently closes work."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from backend.database import get_session_maker
from backend.models import Incident
from backend.settings import get_settings

log = logging.getLogger(__name__)


async def scan_stuck() -> int:
    threshold = datetime.now(timezone.utc) - timedelta(
        seconds=get_settings().stuck_after_seconds
    )
    async with get_session_maker()() as session:
        rows = (await session.scalars(
            select(Incident).where(
                Incident.phase_entered_at < threshold,
                Incident.status_projection.in_(
                    ["DISPATCH_PENDING", "INVESTIGATING", "AWAITING_APPROVAL",
                     "CANCEL_REQUESTED", "REVIEW_ACKNOWLEDGED"]
                ),
            )
            .limit(100)
        )).all()
    for incident in rows:
        log.warning(
            "Incident exceeded phase-age threshold: incident_id=%s phase=%s",
            incident.id,
            incident.status_projection,
        )
    return len(rows)


async def main() -> None:
    while True:
        await scan_stuck()
        await asyncio.sleep(30)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
