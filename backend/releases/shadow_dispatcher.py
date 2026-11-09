"""Stage 11.2 Gate C: Durable Shadow Outbox Dispatcher.

Claims and dispatches RELEASE_SHADOW_EVALUATION outbox items to ShadowExecutor
in isolated background execution without blocking incident progress or exercising
consequential mutation authority.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.observability.metrics import metrics
from backend.persistence.repositories import OutboxRepository
from backend.releases.shadow_executor import ShadowExecutor

logger = logging.getLogger("opsblaze.releases.shadow_dispatcher")


class ShadowOutboxDispatcher:
    """Asynchronous background dispatcher claiming and executing RELEASE_SHADOW_EVALUATION events."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        shadow_executor: ShadowExecutor | None = None,
        *,
        batch_size: int = 10,
        lease_seconds: float = 30.0,
        max_attempts: int = 5,
        base_backoff_seconds: float = 2.0,
    ) -> None:
        self._session_factory = session_factory
        self._outbox_repo = OutboxRepository(session_factory)
        self._shadow_executor = shadow_executor or ShadowExecutor(session_factory)
        self._batch_size = batch_size
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts
        self._base_backoff_seconds = base_backoff_seconds

    async def dispatch_once(self, now: datetime | None = None) -> int:
        """Claims one batch of pending shadow outbox events and executes isolated evaluations."""
        current_time = now or datetime.now(UTC)
        claimed_items = await self._outbox_repo.claim_shadow_batch(
            batch_size=self._batch_size,
            lease_seconds=self._lease_seconds,
            now=current_time,
        )

        if not claimed_items:
            return 0

        processed_count = 0
        for item in claimed_items:
            try:
                incident_id = item.incident_id or uuid.UUID(item.payload.get("incident_id", "00000000-0000-0000-0000-000000000000"))
                result = await self._shadow_executor.execute_shadow_evaluation(
                    tenant_id=item.tenant_id,
                    release_id=item.release_id,
                    incident_id=incident_id,
                )

                logger.info(
                    "Shadow evaluation executed for outbox %d (status=%s, divergence=%s)",
                    item.outbox_id,
                    result.status,
                    result.divergence,
                )

                # Successful processing (even if aborted/discarded per safety rules) acknowledges outbox row
                await self._outbox_repo.mark_outbox_delivered(
                    outbox_id=item.outbox_id,
                    now=current_time,
                )
                metrics.inc("shadow_outbox_delivered_total")
                processed_count += 1

            except Exception as exc:  # noqa: BLE001
                err_msg = f"{type(exc).__name__}: {exc}"
                logger.error(
                    "Error executing shadow outbox item %d (attempt %d/%d): %s",
                    item.outbox_id,
                    item.attempt,
                    self._max_attempts,
                    err_msg,
                )
                if item.attempt >= self._max_attempts:
                    logger.critical(
                        "Shadow outbox item %d exceeded max attempts (%d). Marking dead letter.",
                        item.outbox_id,
                        self._max_attempts,
                    )
                    metrics.inc("shadow_outbox_dead_letter_total")
                    await self._outbox_repo.schedule_retry(
                        outbox_id=item.outbox_id,
                        incident_id=item.incident_id,
                        tenant_id=item.tenant_id,
                        backoff_seconds=86400.0,
                        error_reason=f"DEAD_LETTER: {err_msg}",
                        now=current_time,
                    )
                else:
                    backoff = min(60.0, self._base_backoff_seconds * (2 ** (item.attempt - 1)))
                    await self._outbox_repo.schedule_retry(
                        outbox_id=item.outbox_id,
                        incident_id=item.incident_id,
                        tenant_id=item.tenant_id,
                        backoff_seconds=backoff,
                        error_reason=err_msg,
                        now=current_time,
                    )

        return processed_count

    async def run_forever(
        self,
        stop_event: asyncio.Event,
        poll_interval_seconds: float = 0.5,
    ) -> None:
        """Continuous polling loop with graceful shutdown signal handling."""
        logger.info("Starting ShadowOutboxDispatcher loop (poll_interval=%.2fs)...", poll_interval_seconds)
        while not stop_event.is_set():
            try:
                count = await self.dispatch_once()
                if count == 0:
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=poll_interval_seconds)
                    except TimeoutError:
                        pass
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Unexpected error in ShadowOutboxDispatcher loop")
                await asyncio.sleep(poll_interval_seconds)
        logger.info("ShadowOutboxDispatcher loop stopped cleanly.")
