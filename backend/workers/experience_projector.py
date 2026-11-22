"""E1 idempotent local projector for verified-outcome domain outbox intents."""

import asyncio
import logging
from contextlib import suppress
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import load_only

from backend.experiences import ConsentConflict, capture_capsule
from backend.models import OutcomeRevision

from backend.settings import Settings, get_settings
from backend.telemetry import initialize_tracing, trace_operation
from backend.workers.dead_letters import (
    MAX_PROJECTOR_ATTEMPTS, mark_projection_dead_letter,
)
from backend.workers.heartbeat import heartbeat_loop
from backend.workers.outbox import acknowledge_claim, claim_intent

log = logging.getLogger(__name__)


class InvalidProjectionIntent(ValueError):
    """An immutable outbox payload cannot be interpreted safely."""


async def project_experience_outbox(
    sessions: async_sessionmaker[AsyncSession],
    tenant_id: str, incident_id: str, payload: dict | None,
) -> str:
    """Capture eligible source revision only; never touch a real target."""
    if (
        not isinstance(payload, dict)
        or set(payload) != {"outcome_id"}
        or not isinstance(payload["outcome_id"], str)
    ):
        raise InvalidProjectionIntent("experience projection intent has invalid payload")
    try:
        outcome_id = UUID(payload["outcome_id"])
        source_id = UUID(incident_id)
    except ValueError as exc:
        raise InvalidProjectionIntent("experience projection IDs must be UUIDs") from exc
    if str(outcome_id) != payload["outcome_id"] or str(source_id) != incident_id:
        raise InvalidProjectionIntent("experience projection IDs must be canonical")
    async with sessions() as session:
        latest = await session.scalar(
            select(OutcomeRevision)
            .options(load_only(OutcomeRevision.id, OutcomeRevision.label))
            .where(
                OutcomeRevision.tenant_id == tenant_id,
                OutcomeRevision.incident_id == source_id,
            ).order_by(OutcomeRevision.revision.desc()).limit(1)
        )
        if latest is None or latest.id != outcome_id or latest.label != "VERIFIED":
            return "SUPERSEDED"
    try:
        async with sessions() as session:
            capsule = await capture_capsule(
                session, tenant_id, source_id,
                expected_outcome_id=outcome_id,
            )
    except ConsentConflict:
        # Missing/revoked consent and subsequently revised evidence are not
        # errors in incident recovery. The source cannot be reused, and
        # an operator can explicitly capture later if newly authorized.
        return "INELIGIBLE"
    if capsule is None:
        return "SUPERSEDED"
    return "CAPTURED"


def projector_database_url(settings: Settings) -> str:
    """Refuse to start the standalone worker with general application rights."""
    if not settings.projector_database_url:
        raise ValueError("dedicated projector database URL required")
    primary = make_url(settings.database_url)
    projected = make_url(settings.projector_database_url)
    if (
        projected.drivername != "postgresql+asyncpg"
        or primary.drivername != "postgresql+asyncpg"
        or not projected.username
        or not projected.password
        or projected.username == primary.username
        or projected.database != primary.database
        or projected.host != primary.host
        or projected.port != primary.port
        or projected.username.lower() in {"postgres", "root"}
    ):
        raise ValueError("projector must use distinct scoped PostgreSQL credentials")
    return settings.projector_database_url


async def run_projector_once(
    sessions: async_sessionmaker[AsyncSession],
    *, tenant_filter: str | None = None,
) -> bool:
    """Process one local capsule intent; cannot dispatch a Temporal signal."""
    intent = await claim_intent(
        sessions, tenant_filter=tenant_filter, domain="experience",
    )
    if intent is None:
        return False
    entry_id, tenant_id, incident_id, _kind, payload, attempt = intent
    try:
        with trace_operation(
            "opsblaze.experience.capture", incident_id=UUID(incident_id),
            event_id=entry_id,
        ):
            result = await project_experience_outbox(
                sessions, tenant_id, incident_id, payload,
            )
            if result != "CAPTURED":
                log.info("Experience projection skipped: intent_id=%s state=%s",
                         entry_id, result)
    except Exception as exc:
        code = (
            "INVALID_PAYLOAD" if isinstance(exc, InvalidProjectionIntent)
            else "RETRY_EXHAUSTED"
            if attempt >= MAX_PROJECTOR_ATTEMPTS else None
        )
        # No raw exception, target, evidence, token or source IDs in logs.
        log.warning(
            "Experience projection attempt failed: intent_id=%s attempt=%s code=%s",
            entry_id, attempt, code or "RETRYABLE",
        )
        if code is not None:
            try:
                await mark_projection_dead_letter(
                    sessions, entry_id, tenant_id, attempt, code,
                )
            except Exception:
                log.error("Experience dead-letter store unavailable: intent_id=%s",
                          entry_id)
        return True
    await acknowledge_claim(sessions, intent)
    return True


async def main() -> None:
    settings = get_settings()
    database_url = projector_database_url(settings)
    initialize_tracing(settings)
    engine = create_async_engine(database_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        # PG identity is verified at runtime, not merely trusted from an env var.
        async with engine.connect() as conn:
            login = await conn.scalar(text("SELECT current_user"))
        if login != make_url(database_url).username:
            raise RuntimeError("projector database identity mismatch")
        heartbeat = asyncio.create_task(
            heartbeat_loop("experience_projector", sessions),
        )
        try:
            while True:
                try:
                    processed = await run_projector_once(sessions)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.error("Experience projector store unavailable; retrying")
                    processed = False
                if not processed:
                    await asyncio.sleep(1)
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
    finally:
        await engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
