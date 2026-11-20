"""Remove expired aggregate rate and model-token budget windows.

Only ephemeral counter rows are pruned. The incident, approval, effect,
outcome, and event audit records are outside the deletion boundary.
"""

import argparse
import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.database import get_session_maker
from backend.models import AdmissionUsage, ModelTokenWindow
from backend.settings import get_settings


async def prune_budget_windows(
    sessions: async_sessionmaker[AsyncSession],
    cutoff: datetime,
    batch_size: int = 500,
) -> dict[str, int]:
    if cutoff.tzinfo is None:
        raise ValueError("cutoff must be timezone-aware")
    if not 1 <= batch_size <= 1000:
        raise ValueError("batch size must be 1–1000")
    result: dict[str, int] = {}
    async with sessions() as session:
        async with session.begin():
            for model in (AdmissionUsage, ModelTokenWindow):
                keys = (await session.execute(
                    select(model.tenant_id, model.window_start)
                    .where(model.window_start < cutoff)
                    .order_by(model.window_start, model.tenant_id)
                    .limit(batch_size)
                    .with_for_update(skip_locked=True)
                )).all()
                if keys:
                    await session.execute(
                        delete(model).where(
                            tuple_(model.tenant_id, model.window_start).in_(
                                [(tenant, minute) for tenant, minute in keys]
                            )
                        )
                    )
                result[model.__tablename__] = len(keys)
    return result


async def main() -> None:
    parser = argparse.ArgumentParser(description="Prune ephemeral budget counters")
    parser.add_argument("--days", type=int, default=None)
    args = parser.parse_args()
    days = (
        args.days if args.days is not None
        else get_settings().budget_counter_retention_days
    )
    if not 1 <= days <= 365:
        parser.error("counter retention must be 1–365 days")
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    totals = {"admission_usage": 0, "model_token_window": 0}
    while True:
        removed = await prune_budget_windows(get_session_maker(), cutoff)
        for name, count in removed.items():
            totals[name] += count
        if max(removed.values()) < 500:
            break
    print(f"Pruned ephemeral counters: {totals}")


if __name__ == "__main__":
    asyncio.run(main())
