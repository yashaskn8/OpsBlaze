"""B4 read-only monitor: aged active phases and undelivered outbox intents."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from backend.database import get_session_maker
from backend.models import EffectReceipt, Incident, OutboxEntry
from backend.services import ACTIVE_STATES
from backend.settings import get_settings
from backend.telemetry import initialize_tracing

log = logging.getLogger(__name__)
UNRESOLVED_EFFECT_STATES = ("SENT", "UNKNOWN", "CONFIRMED")


async def scan_stuck() -> int:
    threshold = datetime.now(timezone.utc) - timedelta(
        seconds=get_settings().stuck_after_seconds
    )
    incident_filter = (
        Incident.phase_entered_at < threshold,
        Incident.status_projection.in_(ACTIVE_STATES),
    )
    intent_filter = (
        OutboxEntry.created_at < threshold,
        OutboxEntry.delivered_at.is_(None),
        OutboxEntry.dead_lettered_at.is_(None),
    )
    effect_filter = (
        EffectReceipt.updated_at < threshold,
        EffectReceipt.state.in_(UNRESOLVED_EFFECT_STATES),
    )
    async with get_session_maker()() as session:
        incident_count = int((await session.scalar(
            select(func.count()).select_from(Incident).where(*incident_filter)
        )) or 0)
        intent_count = int((await session.scalar(
            select(func.count()).select_from(OutboxEntry).where(*intent_filter)
        )) or 0)
        effect_count = int((await session.scalar(
            select(func.count()).select_from(EffectReceipt).where(*effect_filter)
        )) or 0)
        incidents = (await session.scalars(
            select(Incident).where(*incident_filter)
            .order_by(Incident.phase_entered_at, Incident.id)
            .limit(100)
        )).all()
        intents = (await session.scalars(
            select(OutboxEntry).where(*intent_filter)
            .order_by(OutboxEntry.created_at, OutboxEntry.id)
            .limit(100)
        )).all()
        effects = (await session.scalars(
            select(EffectReceipt).where(*effect_filter)
            .order_by(EffectReceipt.updated_at, EffectReceipt.id)
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
    for effect in effects:
        log.warning(
            "Unresolved effect exceeded age threshold: effect_id=%s state=%s attempt=%s",
            effect.id, effect.state, effect.attempt_count,
        )
    if (
        incident_count > len(incidents) or intent_count > len(intents)
        or effect_count > len(effects)
    ):
        log.warning(
            "Additional aged items omitted from detailed logs: "
            "incidents=%s outbox=%s effects=%s",
            incident_count - len(incidents),
            intent_count - len(intents), effect_count - len(effects),
        )
    return incident_count + intent_count + effect_count


async def main() -> None:
    initialize_tracing(get_settings())
    while True:
        try:
            await scan_stuck()
        except asyncio.CancelledError:
            raise
        except Exception:
            # An unavailable DB must not silently stop the only age monitor.
            # A fresh session is opened by the next scan; no work is mutated.
            log.exception("B4 state-age scan failed; retrying next interval")
        await asyncio.sleep(30)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
