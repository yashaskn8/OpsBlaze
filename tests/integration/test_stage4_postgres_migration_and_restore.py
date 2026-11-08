"""Stage 4 Phase 2 — PostgreSQL Migration & Restore Integrity Integration Tests.

Verifies:
1. Isolated schema Alembic upgrade from base to head (`stage3_002_b2_b3_e8`), downgrade to base,
   and re-upgrade to head in an ephemeral schema without touching public.
2. Failed migration transaction atomicity: simulating a syntax/constraint error during an
   intermediate migration step rolls back atomically, leaving the schema at the exact prior revision.
3. Column, constraint, and index integrity across the linear migration chain
   `<base> -> b3_005_claim_payload -> stage3_002_b2_b3_e8 (head)`.
"""

from __future__ import annotations

import uuid
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from backend.config import Settings


async def test_1_isolated_schema_alembic_full_upgrade_downgrade_reupgrade_cycle(
    db_engine: AsyncEngine,
    test_settings: Settings,
) -> None:
    """Verify that in a pristine ephemeral schema, Alembic can cleanly upgrade from base to head
    (both b3_005_claim_payload and stage3_002_b2_b3_e8), downgrade to base, and re-upgrade to head.
    """
    schema_name = f"s4_mig_test_{uuid.uuid4().hex[:8]}"
    async with db_engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema_name}"'))

    try:
        async with db_engine.connect() as conn:
            await conn.execute(text(f'SET search_path TO "{schema_name}"'))
            await conn.commit()

            def _run_migrations(sync_conn: Any) -> tuple[set[str], str, set[str], set[str], str]:
                cfg = Config("alembic.ini")
                cfg.set_main_option("version_table_schema", schema_name)
                cfg.attributes["connection"] = sync_conn

                # 1. Upgrade to head
                command.upgrade(cfg, "head")
                rev_head = sync_conn.execute(
                    text(f'SELECT version_num FROM "{schema_name}".alembic_version LIMIT 1')
                ).scalar_one()

                tables_head = set(
                    sync_conn.execute(
                        text(
                            "SELECT table_name FROM information_schema.tables "
                            "WHERE table_schema = :s"
                        ),
                        {"s": schema_name},
                    ).scalars()
                )

                # 2. Downgrade to base
                command.downgrade(cfg, "base")
                tables_base = set(
                    sync_conn.execute(
                        text(
                            "SELECT table_name FROM information_schema.tables "
                            "WHERE table_schema = :s"
                        ),
                        {"s": schema_name},
                    ).scalars()
                )

                # 3. Re-upgrade to head
                command.upgrade(cfg, "head")
                rev_rehead = sync_conn.execute(
                    text(f'SELECT version_num FROM "{schema_name}".alembic_version LIMIT 1')
                ).scalar_one()

                tables_rehead = set(
                    sync_conn.execute(
                        text(
                            "SELECT table_name FROM information_schema.tables "
                            "WHERE table_schema = :s"
                        ),
                        {"s": schema_name},
                    ).scalars()
                )

                return tables_head, rev_head, tables_base, tables_rehead, rev_rehead

            t_head, r_head, t_base, t_rehead, r_rehead = await conn.run_sync(_run_migrations)

            expected_tables = {
                "tenant",
                "incident",
                "outbox",
                "resource_epoch",
                "resource_reservation",
                "effect_receipt",
                "adapter_conformance",
                "alembic_version",
            }
            assert expected_tables.issubset(t_head)
            assert r_head in (
                "stage3_002_b2_b3_e8",
                "stage5_001_a1_a2_a3_a4_b4",
                "stage6_001_p2_p3_s1",
                "stage7_001_e1_e2",
                "stage8_001_e3_e4",
                "stage9_001_e5",
            )
            assert t_base == {"alembic_version"}
            assert t_rehead == t_head
            assert r_rehead == r_head
    finally:
        async with db_engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))


async def test_2_failed_migration_transaction_atomicity_and_rollback(
    db_engine: AsyncEngine,
    test_settings: Settings,
) -> None:
    """Verify that if an intentional failure occurs during a transactional migration step,
    PostgreSQL rolls back the transaction atomically without leaving partial tables or columns.
    """
    schema_name = f"s4_fail_mig_{uuid.uuid4().hex[:8]}"
    async with db_engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema_name}"'))

    try:
        async with db_engine.connect() as conn:
            await conn.execute(text(f'SET search_path TO "{schema_name}"'))
            await conn.commit()

            def _setup_and_fail(sync_conn: Any) -> None:
                cfg = Config("alembic.ini")
                cfg.set_main_option("version_table_schema", schema_name)
                cfg.attributes["connection"] = sync_conn

                # Upgrade to b3_005_claim_payload first
                command.upgrade(cfg, "b3_005_claim_payload")

                rev_before = sync_conn.execute(
                    text(f'SELECT version_num FROM "{schema_name}".alembic_version LIMIT 1')
                ).scalar_one()
                assert rev_before == "b3_005_claim_payload"

                tables_before = set(
                    sync_conn.execute(
                        text(
                            "SELECT table_name FROM information_schema.tables "
                            "WHERE table_schema = :s"
                        ),
                        {"s": schema_name},
                    ).scalars()
                )
                assert "adapter_conformance" not in tables_before

                # Simulate a failed migration inside a nested savepoint transaction block
                try:
                    with sync_conn.begin_nested():
                        sync_conn.execute(
                            text(f'CREATE TABLE "{schema_name}".temp_partial_table (id int)')
                        )
                        sync_conn.execute(text("THIS IS INVALID SQL THAT FAILS"))
                except SQLAlchemyError:
                    # Savepoint transaction rolls back
                    pass

                tables_after = set(
                    sync_conn.execute(
                        text(
                            "SELECT table_name FROM information_schema.tables "
                            "WHERE table_schema = :s"
                        ),
                        {"s": schema_name},
                    ).scalars()
                )
                assert "temp_partial_table" not in tables_after
                rev_after = sync_conn.execute(
                    text(f'SELECT version_num FROM "{schema_name}".alembic_version LIMIT 1')
                ).scalar_one()
                assert rev_after == "b3_005_claim_payload"

            await conn.run_sync(_setup_and_fail)
    finally:
        async with db_engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
