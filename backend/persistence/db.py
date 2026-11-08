from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from backend.config import Settings


def make_engine(
    settings: Settings,
    *,
    pool_size: int = 10,
    max_overflow: int = 10,
    pool_timeout: float = 5.0,
) -> AsyncEngine:
    return create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=pool_timeout,
    )


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # One AsyncSession per concurrent task; never share across coroutines.
    return async_sessionmaker(engine, expire_on_commit=False)
