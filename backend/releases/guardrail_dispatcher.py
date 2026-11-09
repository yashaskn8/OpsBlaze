"""Stage 11.2 Gate B: Durable Guardrail Outbox Dispatcher.

Decouples safety-critical guardrail evaluation and automated rollback from
the B1 incident transition path, ensuring guaranteed at-least-once execution
with worker leases, exponential backoff, and idempotent deduplication.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.domain.release import InvalidReleaseTransitionError, ReleaseNotFoundError
from backend.observability.metrics import emit_event, metrics
from backend.persistence.models import Incident
from backend.persistence.repositories import OutboxRepository
from backend.releases.guardrails import ReleaseGuardrailService

logger = logging.getLogger("opsblaze.releases.guardrail_dispatcher")
GUARDRAIL_OUTBOX_KIND = "RELEASE_GUARDRAIL"


class GuardrailOutboxDispatcher:
    """Asynchronous background worker claiming and executing durable RELEASE_GUARDRAIL outbox events."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        guardrail_service: ReleaseGuardrailService | None = None,
        *,
        batch_size: int = 10,
        lease_seconds: float = 60.0,
        max_attempts: int = 5,
        base_backoff_seconds: float = 1.0,
        poll_interval_seconds: float = 0.5,
        max_backoff_seconds: int = 300,
    ) -> None:
        self._session_factory = session_factory
        self._outbox_repo = OutboxRepository(session_factory)
        self._guardrail_svc = guardrail_service or ReleaseGuardrailService(session_factory)
        self._batch_size = max(1, batch_size)
        self._lease_seconds = max(5.0, float(lease_seconds))
        self._max_attempts = max_attempts
        self._base_backoff_seconds = base_backoff_seconds
        self._poll_interval = max(0.1, poll_interval_seconds)
        self._max_backoff = max(5.0, float(max_backoff_seconds))

    async def dispatch_once(self, *, now: datetime | None = None) -> int:
        """Claims pending guardrail outbox events and executes safety evaluations.

        Returns an integer count of processed items (which evaluates as truthy in boolean checks).
        """
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
            effective_tenant_id: uuid.UUID | None = item.tenant_id
            effective_release_id: uuid.UUID | None = item.release_id
            incident_id = item.incident_id

            # If tenant_id or release_id was not in payload/item, resolve from incident if possible
            if (not effective_tenant_id or not effective_release_id) and incident_id:
                async with self._session_factory() as session:
                    inc = await session.get(Incident, incident_id)
                    if inc is not None:
                        effective_tenant_id = effective_tenant_id or inc.tenant_id
                        effective_release_id = effective_release_id or inc.policy_release_id

            if not effective_release_id or not effective_tenant_id:
                # No release pinned to this incident; acknowledge and complete
                await self._outbox_repo.mark_outbox_delivered(
                    outbox_id=item.outbox_id,
                    now=current_time,
                )
                processed_count += 1
                continue

            try:
                rolled_back, reason = await self._guardrail_svc.evaluate_and_enforce_guardrails(
                    tenant_id=effective_tenant_id,
                    release_id=effective_release_id,
                )
                if rolled_back:
                    logger.warning(
                        "Durable guardrail dispatcher rolled back release %s: %s",
                        effective_release_id,
                        reason,
                    )
                    metrics.inc("guardrail_automated_rollbacks_total")

                await self._outbox_repo.mark_outbox_delivered(
                    outbox_id=item.outbox_id,
                    now=current_time,
                )
                metrics.inc("guardrail_outbox_delivered_total")
                metrics.inc("release_guardrail_jobs_completed_total")
                emit_event(
                    "release.guardrail_job.completed",
                    outbox_id=item.outbox_id,
                    incident_id=incident_id,
                    release_id=effective_release_id,
                    attempt=item.attempt,
                    rolled_back=rolled_back,
                    reason=reason[:512],
                )
                processed_count += 1

            except (InvalidReleaseTransitionError, ReleaseNotFoundError) as term_err:
                logger.info(
                    "Guardrail outbox item %d for release %s reached terminal state: %s. Acknowledging.",
                    item.outbox_id,
                    effective_release_id,
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
                    err_msg = f"ValueError: {val_err}"
                    await self._handle_retry(item, err_msg, current_time)

            except Exception as exc:  # noqa: BLE001
                err_msg = f"{type(exc).__name__}: {exc}"
                logger.error(
                    "Error executing guardrail outbox item %d (attempt %d/%d): %s",
                    item.outbox_id,
                    item.attempt,
                    self._max_attempts,
                    err_msg,
                )
                await self._handle_retry(item, err_msg, current_time)

        return processed_count

    async def _handle_retry(self, item: Any, err_msg: str, current_time: datetime) -> None:
        if item.attempt >= self._max_attempts:
            logger.critical(
                "Guardrail outbox item %d exceeded max attempts (%d). Marking dead letter.",
                item.outbox_id,
                self._max_attempts,
            )
            metrics.inc("guardrail_outbox_dead_letter_total")
            await self._outbox_repo.schedule_retry(
                outbox_id=item.outbox_id,
                incident_id=item.incident_id,
                tenant_id=item.tenant_id,
                backoff_seconds=86400.0,
                error_reason=f"DEAD_LETTER: {err_msg}",
                now=current_time,
            )
        else:
            backoff = min(self._max_backoff, self._base_backoff_seconds * (2 ** (item.attempt - 1)))
            await self._outbox_repo.schedule_retry(
                outbox_id=item.outbox_id,
                incident_id=item.incident_id,
                tenant_id=item.tenant_id,
                backoff_seconds=backoff,
                error_reason=err_msg,
                now=current_time,
            )
            metrics.inc("release_guardrail_jobs_retried_total")
            emit_event(
                "release.guardrail_job.retry",
                outbox_id=item.outbox_id,
                incident_id=item.incident_id,
                attempt=item.attempt,
                backoff_seconds=backoff,
                error=err_msg[:512],
                level="ERROR",
            )

    async def run_forever(
        self,
        stop_event: asyncio.Event,
        poll_interval_seconds: float | None = None,
    ) -> None:
        poll_sec = poll_interval_seconds or self._poll_interval
        logger.info("Starting GuardrailOutboxDispatcher loop (poll_interval=%.2fs)...", poll_sec)
        while not stop_event.is_set():
            try:
                count = await self.dispatch_once()
                if count == 0:
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=poll_sec)
                    except TimeoutError:
                        pass
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Unexpected error in GuardrailOutboxDispatcher loop")
                await asyncio.sleep(poll_sec)
        logger.info("GuardrailOutboxDispatcher loop stopped cleanly.")


ReleaseGuardrailDispatcher = GuardrailOutboxDispatcher
