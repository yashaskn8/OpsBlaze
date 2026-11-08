"""B4 Durable Budget & Capacity Reservation Manager."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.domain.budget import (
    BudgetConcurrencyError,
    BudgetExhaustedError,
    BudgetLimits,
    BudgetReservation,
    BudgetReservationNotFoundError,
    BudgetSnapshot,
    BudgetStatus,
)
from backend.persistence.models import BudgetLedger


class DurableBudgetManager:
    """Manages two-phase budget reservations, capacity limits, and row-locked settlements."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def ensure_budget(
        self,
        tenant_id: uuid.UUID,
        incident_id: uuid.UUID,
        limits: BudgetLimits | None = None,
    ) -> BudgetSnapshot:
        """Initialize or retrieve the budget ledger for an incident."""
        actual_limits = limits or BudgetLimits()
        async with self._session_factory() as session:
            stmt = select(BudgetLedger).where(
                BudgetLedger.tenant_id == tenant_id,
                BudgetLedger.incident_id == incident_id,
            )
            res = await session.execute(stmt)
            row = res.scalar_one_or_none()
            if row is None:
                row = BudgetLedger(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    incident_id=incident_id,
                    reserved_tokens=0,
                    settled_tokens=0,
                    reserved_probes=0,
                    settled_probes=0,
                    reserved_cost_micros=0,
                    settled_cost_micros=0,
                    max_tokens=actual_limits.max_tokens,
                    max_probes=actual_limits.max_probes,
                    max_cost_micros=actual_limits.max_cost_micros,
                    max_time_seconds=actual_limits.max_time_seconds,
                    status=BudgetStatus.ACTIVE.value,
                )
                session.add(row)
                await session.commit()
                await session.refresh(row)

            return BudgetSnapshot.model_validate(row)

    async def reserve_budget(
        self,
        tenant_id: uuid.UUID,
        incident_id: uuid.UUID,
        tokens: int,
        probes: int,
        cost_micros: int,
        lease_seconds: int = 60,
    ) -> BudgetReservation:
        """Acquire a lease-fenced budget reservation under a SELECT FOR UPDATE lock."""
        async with self._session_factory() as session:
            stmt = (
                select(BudgetLedger)
                .where(
                    BudgetLedger.tenant_id == tenant_id,
                    BudgetLedger.incident_id == incident_id,
                )
                .with_for_update()
            )
            res = await session.execute(stmt)
            row = res.scalar_one_or_none()
            if row is None:
                raise BudgetReservationNotFoundError(
                    f"No budget ledger found for incident {incident_id}"
                )

            now = datetime.now(UTC)

            # Check existing reservation lease
            if row.reservation_nonce is not None and row.lease_expires_at is not None:
                lease_exp = row.lease_expires_at
                if lease_exp.tzinfo is None:
                    lease_exp = lease_exp.replace(tzinfo=UTC)

                if lease_exp < now:
                    # Auto-reclaim expired lease
                    row.reserved_tokens = 0
                    row.reserved_probes = 0
                    row.reserved_cost_micros = 0
                    row.reservation_nonce = None
                    row.lease_expires_at = None
                else:
                    raise BudgetConcurrencyError(
                        f"Active unexpired reservation lease already held: nonce={row.reservation_nonce}"
                    )

            # Check ceilings
            proj_tokens = row.settled_tokens + tokens
            proj_probes = row.settled_probes + probes
            proj_cost = row.settled_cost_micros + cost_micros

            if proj_tokens > row.max_tokens:
                row.status = BudgetStatus.EXHAUSTED.value
                await session.commit()
                raise BudgetExhaustedError("tokens", proj_tokens, row.max_tokens)

            if proj_probes > row.max_probes:
                row.status = BudgetStatus.EXHAUSTED.value
                await session.commit()
                raise BudgetExhaustedError("probes", proj_probes, row.max_probes)

            if proj_cost > row.max_cost_micros:
                row.status = BudgetStatus.EXHAUSTED.value
                await session.commit()
                raise BudgetExhaustedError("cost_micros", proj_cost, row.max_cost_micros)

            nonce = uuid.uuid4()
            lease_expires_at = now + timedelta(seconds=lease_seconds)

            row.reserved_tokens = tokens
            row.reserved_probes = probes
            row.reserved_cost_micros = cost_micros
            row.reservation_nonce = nonce
            row.lease_expires_at = lease_expires_at

            await session.commit()

            return BudgetReservation(
                nonce=nonce,
                tokens=tokens,
                probes=probes,
                cost_micros=cost_micros,
                lease_expires_at=lease_expires_at,
            )

    async def settle_budget(
        self,
        tenant_id: uuid.UUID,
        incident_id: uuid.UUID,
        nonce: uuid.UUID,
        actual_tokens: int,
        actual_probes: int,
        actual_cost_micros: int,
    ) -> BudgetSnapshot:
        """Settle actual usage against a reservation, reclaiming the unused delta."""
        async with self._session_factory() as session:
            stmt = (
                select(BudgetLedger)
                .where(
                    BudgetLedger.tenant_id == tenant_id,
                    BudgetLedger.incident_id == incident_id,
                )
                .with_for_update()
            )
            res = await session.execute(stmt)
            row = res.scalar_one_or_none()
            if row is None:
                raise BudgetReservationNotFoundError(
                    f"No budget ledger found for incident {incident_id}"
                )

            if row.reservation_nonce != nonce:
                raise BudgetReservationNotFoundError(
                    f"Reservation nonce {nonce} mismatch or expired (current={row.reservation_nonce})"
                )

            # Apply actuals and clear reservation
            row.settled_tokens += actual_tokens
            row.settled_probes += actual_probes
            row.settled_cost_micros += actual_cost_micros

            row.reserved_tokens = 0
            row.reserved_probes = 0
            row.reserved_cost_micros = 0
            row.reservation_nonce = None
            row.lease_expires_at = None

            if (
                row.settled_tokens >= row.max_tokens
                or row.settled_probes >= row.max_probes
                or row.settled_cost_micros >= row.max_cost_micros
            ):
                row.status = BudgetStatus.EXHAUSTED.value

            await session.commit()
            await session.refresh(row)

            return BudgetSnapshot.model_validate(row)

    async def get_snapshot(self, tenant_id: uuid.UUID, incident_id: uuid.UUID) -> BudgetSnapshot:
        """Retrieve current snapshot of the incident budget."""
        async with self._session_factory() as session:
            stmt = select(BudgetLedger).where(
                BudgetLedger.tenant_id == tenant_id,
                BudgetLedger.incident_id == incident_id,
            )
            res = await session.execute(stmt)
            row = res.scalar_one_or_none()
            if row is None:
                raise BudgetReservationNotFoundError(
                    f"No budget ledger found for incident {incident_id}"
                )
            return BudgetSnapshot.model_validate(row)
