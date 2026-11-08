"""Durable outbox dispatcher for isolated E6 shadow evaluations."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.observability.metrics import emit_event, metrics
from backend.persistence.models import Incident, Outbox
from backend.releases.router_service import PolicyRouterService

logger = logging.getLogger("opsblaze.releases.shadow_dispatcher")
SHADOW_OUTBOX_KIND = "RELEASE_SHADOW_EVALUATION"


class ShadowEvaluationDispatcher:
    """Leased, retryable shadow evaluator; failures never affect incumbent incident execution."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        poll_interval_seconds: float = 0.5,
        lease_seconds: int = 60,
        max_backoff_seconds: int = 300,
    ) -> None:
        self._session_factory = session_factory
        self._poll_interval = max(0.1, poll_interval_seconds)
        self._lease_seconds = max(10, lease_seconds)
        self._max_backoff = max(5, max_backoff_seconds)
        self._router = PolicyRouterService(session_factory)

    async def dispatch_once(self, *, now: datetime | None = None) -> bool:
        current = now or datetime.now(UTC)
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(Outbox, Incident)
                    .join(Incident, Outbox.incident_id == Incident.id)
                    .where(
                        Outbox.kind == SHADOW_OUTBOX_KIND,
                        Outbox.delivered_at.is_(None),
                        Outbox.available_at <= current,
                    )
                    .order_by(Outbox.available_at.asc(), Outbox.id.asc())
                    .limit(1)
                    .with_for_update(of=Outbox, skip_locked=True)
                )
            ).first()
            if row is None:
                return False
            outbox, incident = row
            outbox.attempt += 1
            outbox.available_at = current + timedelta(seconds=self._lease_seconds)
            outbox_id = outbox.id
            tenant_id = incident.tenant_id
            incident_id = incident.id
            target_id = incident.target_id
            release_id = incident.policy_release_id
            attempt = outbox.attempt
            await session.commit()

        try:
            if release_id is None:
                raise ValueError("Shadow outbox event has no pinned policy release.")
            result = await self._router.execute_shadow_observation(
                tenant_id=tenant_id,
                incident_id=incident_id,
                target_id=target_id,
                release_id=release_id,
            )
            async with self._session_factory() as session:
                pending = await session.get(Outbox, outbox_id)
                if pending is not None and pending.delivered_at is None:
                    pending.delivered_at = datetime.now(UTC)
                    pending.last_error = None
                    await session.commit()
            metrics.inc("release_shadow_jobs_completed_total")
            emit_event(
                "release.shadow_job.completed",
                outbox_id=outbox_id,
                incident_id=incident_id,
                release_id=release_id,
                attempt=attempt,
                shadow_status=result["shadow_status"],
            )
            return True
        except Exception as exc:
            backoff = min(self._max_backoff, 2 ** min(attempt, 8))
            async with self._session_factory() as session:
                pending = await session.get(Outbox, outbox_id)
                if pending is not None and pending.delivered_at is None:
                    pending.available_at = datetime.now(UTC) + timedelta(seconds=backoff)
                    pending.last_error = f"{type(exc).__name__}: {exc}"[:2000]
                    await session.commit()
            metrics.inc("release_shadow_jobs_retried_total")
            emit_event(
                "release.shadow_job.retry",
                outbox_id=outbox_id,
                incident_id=incident_id,
                attempt=attempt,
                backoff_seconds=backoff,
                error=f"{type(exc).__name__}: {exc}"[:512],
                level="ERROR",
            )
            logger.exception("Shadow evaluation outbox item %s failed and was rescheduled", outbox_id)
            return False

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                worked = await self.dispatch_once()
            except (SQLAlchemyError, OSError, RuntimeError):
                logger.exception("Shadow evaluation outbox polling failed")
                worked = False
            if worked:
                continue
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._poll_interval)
            except TimeoutError:
                pass
