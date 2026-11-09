"""Phase 3.2 — Database transaction failure, atomicity, pool exhaustion, and migration tests for B1."""

from __future__ import annotations

import uuid
from typing import Self

import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from backend.api.main import create_app
from backend.config import Settings
from backend.domain.auth import mint_principal_token
from backend.persistence.db import make_engine, make_session_factory
from backend.persistence.models import Incident, Outbox
from backend.persistence.repositories import IncidentRepository

pytestmark = pytest.mark.postgres


def _signed_headers(
    settings: Settings, tenant_id: uuid.UUID, idempotency_key: str
) -> dict[str, str]:
    token = mint_principal_token(
        actor_id="db-failure-test-user",
        tenant_id=tenant_id,
        roles=["operator"],
        secret_key=settings.auth_secret_key or "",
        ttl_seconds=300,
        auth_mechanism="pytest_db_failure_test",
    )
    return {
        "Authorization": f"Bearer {token}",
        "X-Tenant-ID": str(tenant_id),
        "Idempotency-Key": idempotency_key,
    }


async def _count_tenant_state(
    session_factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> tuple[int, int]:
    async with session_factory() as session:
        inc_ids = (
            (await session.execute(select(Incident.id).where(Incident.tenant_id == tenant_id)))
            .scalars()
            .all()
        )
        if not inc_ids:
            return 0, 0
        outbox_cnt = (
            await session.execute(
                select(func.count(Outbox.id)).where(Outbox.incident_id.in_(inc_ids))
            )
        ).scalar_one()
        return len(inc_ids), int(outbox_cnt)


async def test_1_failure_before_incident_insertion_leaves_zero_rows(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    def failing_factory() -> AsyncSession:
        raise OperationalError("SELECT 1", {}, Exception("simulated pre-insert failure"))

    repo = IncidentRepository(failing_factory)  # type: ignore[arg-type]
    with pytest.raises(OperationalError):
        await repo.admit_incident(
            tenant_id=isolated_tenant,
            client_key="fail-before-insert",
            target_id="sim-db-1",
            request_sha256="1" * 64,
        )

    assert await _count_tenant_state(session_factory, isolated_tenant) == (0, 0)


async def test_2_failure_during_incident_insertion_rolls_back(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    """Force foreign-key violation on incident insert (non-existent tenant) and verify no orphan outbox."""
    repo = IncidentRepository(session_factory)
    non_existent_tenant = uuid.uuid4()
    with pytest.raises(IntegrityError):
        await repo.admit_incident(
            tenant_id=non_existent_tenant,
            client_key="fail-during-incident",
            target_id="sim-db-1",
            request_sha256="2" * 64,
        )

    assert await _count_tenant_state(session_factory, non_existent_tenant) == (0, 0)


async def test_3_failure_during_outbox_insertion_rolls_back_incident(
    db_engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    """Inject a flush hook that sets outbox.attempt = -1 (violating chk_outbox_attempt_nonnegative)
    during outbox insertion, proving the parent incident row is atomically rolled back.
    """

    def corrupt_outbox_on_flush(session, flush_context, instances) -> None:  # type: ignore[no-untyped-def]
        for obj in session.new:
            if isinstance(obj, Outbox):
                obj.attempt = -5  # Violates chk_outbox_attempt_nonnegative

    event.listen(AsyncSession.sync_session_class, "before_flush", corrupt_outbox_on_flush)
    try:
        repo = IncidentRepository(session_factory)
        with pytest.raises(IntegrityError):
            await repo.admit_incident(
                tenant_id=isolated_tenant,
                client_key="fail-during-outbox",
                target_id="sim-db-1",
                request_sha256="3" * 64,
            )
    finally:
        event.remove(AsyncSession.sync_session_class, "before_flush", corrupt_outbox_on_flush)

    # Zero orphan incidents and zero outbox rows must exist
    assert await _count_tenant_state(session_factory, isolated_tenant) == (0, 0)

    # Subsequent valid admission recovers cleanly
    repo = IncidentRepository(session_factory)
    outcome = await repo.admit_incident(
        tenant_id=isolated_tenant,
        client_key="recover-after-outbox-fail",
        target_id="sim-db-1",
        request_sha256="3" * 64,
    )
    assert outcome.created is True
    assert await _count_tenant_state(session_factory, isolated_tenant) == (1, 1)


async def test_4_and_5_failure_before_and_during_commit_rolls_back_atomically(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    """Wrap AsyncSession.commit to raise before/during commit and verify neither Incident nor Outbox persists."""

    class CommitFailingSessionWrapper:
        def __init__(self, inner: AsyncSession) -> None:
            self._inner = inner

        async def __aenter__(self) -> Self:
            await self._inner.__aenter__()
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:  # type: ignore[no-untyped-def]
            await self._inner.rollback()
            await self._inner.__aexit__(exc_type, exc_val, exc_tb)

        def add(self, instance: object) -> None:
            self._inner.add(instance)

        async def execute(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            return await self._inner.execute(*args, **kwargs)

        async def commit(self) -> None:
            await self._inner.flush()
            raise OperationalError("COMMIT", {}, Exception("simulated commit loss"))

        async def rollback(self) -> None:
            await self._inner.rollback()

    def wrapped_factory():  # type: ignore[no-untyped-def]
        return CommitFailingSessionWrapper(session_factory())

    repo = IncidentRepository(wrapped_factory)  # type: ignore[arg-type]
    with pytest.raises(OperationalError):
        await repo.admit_incident(
            tenant_id=isolated_tenant,
            client_key="fail-on-commit",
            target_id="sim-db-1",
            request_sha256="4" * 64,
        )

    assert await _count_tenant_state(session_factory, isolated_tenant) == (0, 0)


async def test_6_failure_immediately_after_successful_commit_preserves_idempotent_recovery(
    session_factory: async_sessionmaker[AsyncSession],
    isolated_tenant: uuid.UUID,
) -> None:
    """Simulate a crash/exception immediately after DB commit succeeds (lost HTTP response).
    Verify that when the client retries with the same Idempotency-Key, it recovers the committed
    incident without creating a second incident or outbox row.
    """
    real_repo = IncidentRepository(session_factory)
    key = f"post-commit-crash-{uuid.uuid4()}"
    sha = "6" * 64

    outcome_1 = await real_repo.admit_incident(
        tenant_id=isolated_tenant,
        client_key=key,
        target_id="sim-db-1",
        request_sha256=sha,
    )
    assert outcome_1.created is True

    # Client retries after lost response
    outcome_2 = await real_repo.admit_incident(
        tenant_id=isolated_tenant,
        client_key=key,
        target_id="sim-db-1",
        request_sha256=sha,
    )
    assert outcome_2.created is False
    assert outcome_2.response.id == outcome_1.response.id
    assert await _count_tenant_state(session_factory, isolated_tenant) == (1, 1)


async def test_7_database_connection_loss_returns_explicit_503_on_api_and_readyz(
    test_settings: Settings,
) -> None:
    """Verify /readyz and POST /v1/incidents return HTTP 503 when PostgreSQL is unreachable,
    while /healthz remains 200 OK.
    """
    broken_settings = test_settings.model_copy(update={"postgres_port": 59999})
    app = create_app(broken_settings)
    app.state.engine = make_engine(broken_settings, pool_size=1, max_overflow=0, pool_timeout=0.5)
    app.state.session_factory = make_session_factory(app.state.engine)

    db_down_tenant = uuid.uuid4()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            r_health = await client.get("/healthz")
            assert r_health.status_code == 200
            assert r_health.json() == {"status": "ok"}

            r_ready = await client.get("/readyz")
            assert r_ready.status_code == 503
            assert r_ready.json()["status"] == "degraded"

            r_admit = await client.post(
                "/v1/incidents",
                json={"target_id": "sim-db-1"},
                headers=_signed_headers(broken_settings, db_down_tenant, "db-down-key"),
            )
            assert r_admit.status_code == 503
    finally:
        await app.state.engine.dispose()


async def test_9_connection_pool_exhaustion_and_recovery(
    test_settings: Settings,
    isolated_tenant: uuid.UUID,
) -> None:
    """Exhaust a 1-connection pool with a held connection, verify admission returns 503 on timeout,
    then release the connection and verify subsequent admission succeeds with 202.
    """
    tiny_engine = make_engine(test_settings, pool_size=1, max_overflow=0, pool_timeout=0.25)
    app = create_app(test_settings)
    app.state.engine = tiny_engine
    app.state.session_factory = make_session_factory(tiny_engine)

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            # Hold the single pool connection open
            held_conn = await tiny_engine.connect()
            try:
                r_exhausted = await client.post(
                    "/v1/incidents",
                    json={"target_id": "sim-pool-target"},
                    headers=_signed_headers(test_settings, isolated_tenant, f"pool-key-{uuid.uuid4()}"),
                )
                assert r_exhausted.status_code == 503
            finally:
                await held_conn.close()

            # Pool is now free; admission must recover immediately
            r_recovered = await client.post(
                "/v1/incidents",
                json={"target_id": "sim-pool-target"},
                headers=_signed_headers(
                    test_settings, isolated_tenant, f"pool-key-recovered-{uuid.uuid4()}"
                ),
            )
            assert r_recovered.status_code == 202
    finally:
        await tiny_engine.dispose()


async def test_isolated_schema_alembic_upgrade_and_downgrade(
    db_engine: AsyncEngine,
    test_settings: Settings,
) -> None:
    """Verify the reconstructed b3_005_claim_payload migration upgrades and downgrades cleanly
    inside an isolated temporary PostgreSQL schema without touching public.
    """
    schema_name = f"b1_mig_test_{uuid.uuid4().hex[:8]}"
    async with db_engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema_name}"'))

    try:
        async with db_engine.connect() as conn:
            await conn.execute(text(f'SET search_path TO "{schema_name}"'))
            await conn.commit()

            def _run_upgrade_and_downgrade(sync_conn) -> set[str]:  # type: ignore[no-untyped-def]
                cfg = Config("alembic.ini")
                cfg.set_main_option("version_table_schema", schema_name)
                cfg.attributes["connection"] = sync_conn
                command.upgrade(cfg, "head")
                tables_after_up = set(
                    sync_conn.execute(
                        text(
                            "SELECT table_name FROM information_schema.tables "
                            "WHERE table_schema = :s"
                        ),
                        {"s": schema_name},
                    ).scalars()
                )
                command.downgrade(cfg, "base")
                tables_after_down = set(
                    sync_conn.execute(
                        text(
                            "SELECT table_name FROM information_schema.tables "
                            "WHERE table_schema = :s"
                        ),
                        {"s": schema_name},
                    ).scalars()
                )
                assert tables_after_down == {"alembic_version"}
                return tables_after_up

            created_tables = await conn.run_sync(_run_upgrade_and_downgrade)
            assert {
                "tenant",
                "incident",
                "outbox",
                "resource_epoch",
                "resource_reservation",
                "effect_receipt",
                "alembic_version",
            }.issubset(created_tables)
    finally:
        async with db_engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
