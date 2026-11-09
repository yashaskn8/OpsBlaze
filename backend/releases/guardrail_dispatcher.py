"""Stage 11.2 Gate B: Durable Guardrail Outbox Dispatcher.

Decouples safety-critical guardrail evaluation and automated rollback from
the B1 incident transition path, ensuring guaranteed at-least-once execution
with worker leases, exponential backoff, and idempotent deduplication.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.domain.release import InvalidReleaseTransitionError, ReleaseNotFoundError
from backend.observability.metrics import metrics
from backend.persistence.repositories import OutboxRepository
from backend.releases.guardrails import ReleaseGuardrailService

logger = logging.getLogger("opsblaze.releases.guardrail_dispatcher")


class GuardrailOutboxDispatcher:
    """Asynchronous background worker claiming and executing durable RELEASE_GUARDRAIL outbox events."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        guardrail_service: ReleaseGuardrailService | None = None,
        *,
        batch_size: int = 10,
        lease_seconds: float = 15.0,
        max_attempts: int = 5,
        base_backoff_seconds: float = 1.0,
    ) -> None:
        self._session_factory = session_factory
        self._outbox_repo = OutboxRepository(session_factory)
        self._guardrail_svc = guardrail_service or ReleaseGuardrailService(session_factory)
        self._batch_size = batch_size
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts
        self._base_backoff_seconds = base_backoff_seconds

    async def dispatch_once(self, now: datetime | None = None) -> int:
        """Claims one batch of pending guardrail outbox events and executes safety evaluations."""
        current_time = now or datetime.now(UTC)
        claimed_items = await self._outbox_repo.claim_guardrail_batch(
            batch_size=self._batch_size,
            lease_seconds=self._lease_seconds,
            now=current_time,
        )

        if not claimed_items:
            return 0

        processed_count = 0
        for item in claimed_items:
            try:
                rolled_back, reason = await self._guardrail_svc.evaluate_and_enforce_guardrails(
                    tenant_id=item.tenant_id,
                    release_id=item.release_id,
                )
                if rolled_back:
                    logger.warning(
                        "Durable guardrail dispatcher rolled back release %s: %s",
                        item.release_id,
                        reason,
                    )
                    metrics.inc("guardrail_automated_rollbacks_total")

                await self._outbox_repo.mark_outbox_delivered(
                    outbox_id=item.outbox_id,
                    now=current_time,
                )
                metrics.inc("guardrail_outbox_delivered_total")
                processed_count += 1

            except (InvalidReleaseTransitionError, ReleaseNotFoundError) as term_err:
                # Terminal release state (e.g. already rolled back/cancelled) is an idempotent success
                logger.info(
                    "Guardrail outbox item %d for release %s reached terminal state: %s. Acknowledging.",
                    item.outbox_id,
                    item.release_id,
                    term_err,
                )
                await self._outbox_repo.mark_outbox_delivered(
                    outbox_id=item.outbox_id,
                    now=current_time,
                )
                processed_count += 1

            except ValueError as val_err:
                if "not found" in str(val_err).lower():
                    logger.info(
                        "Guardrail outbox item %d target not found: %s. Acknowledging.",
                        item.outbox_id,
                        val_err,
                    )
                    await self._outbox_repo.mark_outbox_delivered(
                        outbox_id=item.outbox_id,
                        now=current_time,
                    )
                    processed_count += 1
                else:
                    raise

            except Exception as exc:  # noqa: BLE001
                err_msg = f"{type(exc).__name__}: {exc}"
                logger.error(
                    "Error executing guardrail outbox item %d (attempt %d/%d): %s",
                    item.outbox_id,
                    item.attempt,
                    self._max_attempts,
                    err_msg,
                )
                if item.attempt >= self._max_attempts:
                    logger.critical(
                        "Guardrail outbox item %d exceeded max attempts (%d). Marking dead letter.",
                        item.outbox_id,
                        self._max_attempts,
                    )
                    metrics.inc("guardrail_outbox_dead_letter_total")
                    # Acknowledge or preserve error with high backoff to avoid hot-looping
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
        logger.info("Starting GuardrailOutboxDispatcher loop (poll_interval=%.2fs)...", poll_interval_seconds)
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
                logger.exception("Unexpected error in GuardrailOutboxDispatcher loop")
                await asyncio.sleep(poll_interval_seconds)
        logger.info("GuardrailOutboxDispatcher loop stopped cleanly.")
