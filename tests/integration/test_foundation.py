import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from backend.api.main import app
from backend.config import get_settings
from backend.persistence.db import make_engine


async def test_healthz() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/healthz")).json() == {"status": "ok"}


@pytest.mark.postgres
async def test_postgres_identity() -> None:
    engine = make_engine(get_settings())
    async with engine.connect() as conn:
        row = (await conn.execute(text("select current_database(), current_user"))).one()
    await engine.dispose()
    assert row[0] == get_settings().postgres_db
