
"""Atomic, per-tenant minute token reservations for optional model calls."""

from datetime import datetime, timezone

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import ModelTokenWindow
from backend.settings import get_settings


def current_window() -> datetime:
    return datetime.now(timezone.utc).replace(second=0, microsecond=0)


async def reserve_model_tokens(
    session: AsyncSession, tenant_id: str,
    window_start: datetime, requested_tokens: int,
) -> bool:
    limit = get_settings().ai_token_limit_per_minute
    if limit < 1 or requested_tokens < 1:
        raise ValueError("token budgets must be positive")
    if requested_tokens > limit:
        return False
    if session.bind.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif session.bind.dialect.name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:
        raise RuntimeError("unsupported model budget database")

    await session.execute(
        insert(ModelTokenWindow)
        .values(
            tenant_id=tenant_id, window_start=window_start,
            reserved_tokens=0, used_tokens=0,
        )
        .on_conflict_do_nothing(index_elements=["tenant_id", "window_start"])
    )
    result = await session.execute(
        update(ModelTokenWindow)
        .where(
            ModelTokenWindow.tenant_id == tenant_id,
            ModelTokenWindow.window_start == window_start,
            ModelTokenWindow.reserved_tokens + requested_tokens <= limit,
            ModelTokenWindow.used_tokens + requested_tokens <= limit,
        )
        .values(
            reserved_tokens=ModelTokenWindow.reserved_tokens + requested_tokens,
        )
    )
    return result.rowcount == 1


async def settle_model_tokens(
    session: AsyncSession, tenant_id: str,
    window_start: datetime | None, actual_tokens: int,
) -> None:
    if window_start is None:
        return  # Legacy pre-budget advisory rows have no settlement window.
    await session.execute(
        update(ModelTokenWindow)
        .where(
            ModelTokenWindow.tenant_id == tenant_id,
            ModelTokenWindow.window_start == window_start,
        )
        .values(
            used_tokens=ModelTokenWindow.used_tokens + max(0, actual_tokens),
        )
    )
