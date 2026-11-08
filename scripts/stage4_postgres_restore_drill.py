"""Stage 4 Phase 2 — PostgreSQL Backup, Isolated Restore & Forward-Migration Drill.

Executes real pg_dump / pg_restore / Alembic upgrade drills against isolated temporary
databases without ever modifying or overwriting `opsblaze` or `opsblaze_test`.
Writes deterministic evidence to `docs/evidence/stage4-postgres-restore-drill.json`.
"""
# ruff: noqa: ASYNC221

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config import get_settings

PG_DUMP = r"C:\Program Files\PostgreSQL\17\bin\pg_dump.exe"
PG_RESTORE = r"C:\Program Files\PostgreSQL\17\bin\pg_restore.exe"
PROTECTED_DBS = frozenset({"opsblaze", "opsblaze_test", "postgres", "template0", "template1"})


def _make_async_engine(db_name: str, *, autocommit: bool = False) -> AsyncEngine:
    s = get_settings().model_copy(update={"postgres_db": db_name})
    kwargs: dict[str, Any] = {"pool_pre_ping": True}
    if autocommit:
        kwargs["isolation_level"] = "AUTOCOMMIT"
    return create_async_engine(s.database_url, **kwargs)


def _pg_env() -> dict[str, str]:
    s = get_settings()
    env = dict(os.environ)
    env["PGPASSWORD"] = s.postgres_password
    return env


async def inspect_database_fingerprint(db_name: str) -> dict[str, Any]:
    """Capture complete schema, constraints, indexes, sequences, foreign keys, migration
    version, and non-sensitive SHA-256 row fingerprints for a database.
    """
    eng = _make_async_engine(db_name)
    try:
        async with eng.connect() as conn:
            tables = [
                r[0]
                for r in (
                    await conn.execute(
                        text(
                            """
                            SELECT table_name
                            FROM information_schema.tables
                            WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
                            ORDER BY table_name
                            """
                        )
                    )
                ).fetchall()
            ]

            columns = [
                {
                    "table": r[0],
                    "column": r[1],
                    "data_type": r[2],
                    "is_nullable": r[3],
                }
                for r in (
                    await conn.execute(
                        text(
                            """
                            SELECT table_name, column_name, data_type, is_nullable
                            FROM information_schema.columns
                            WHERE table_schema = 'public'
                            ORDER BY table_name, ordinal_position
                            """
                        )
                    )
                ).fetchall()
            ]

            def _norm_pg_expr(defn: str) -> str:
                import re

                # Normalize PostgreSQL pg_dump's element-wise ::text cast in ARRAY[...] to match ::text[]
                def _repl_array(m: re.Match[str]) -> str:
                    inner = m.group(1)
                    cleaned = re.sub(
                        r"\(('[^']+'::character varying)\)::text",
                        r"\1",
                        inner,
                    )
                    return f"(ARRAY[{cleaned}])::text[]"

                res = re.sub(r"ARRAY\[([^\]]+)\]", _repl_array, defn)
                while "((ARRAY[" in res:
                    res = res.replace("((ARRAY[", "(ARRAY[")
                while "])::text[])::text[]" in res:
                    res = res.replace("])::text[])::text[]", "])::text[]")
                return res

            constraints = [
                {
                    "table": str(r[0]),
                    "name": str(r[1]),
                    "type": r[2].decode("utf-8") if isinstance(r[2], bytes) else str(r[2]),
                    "definition": _norm_pg_expr(str(r[3])),
                }
                for r in (
                    await conn.execute(
                        text(
                            """
                            SELECT rel.relname AS table_name,
                                   con.conname AS constraint_name,
                                   con.contype AS constraint_type,
                                   pg_get_constraintdef(con.oid) AS definition
                            FROM pg_constraint con
                            JOIN pg_class rel ON rel.oid = con.conrelid
                            JOIN pg_namespace nsp ON nsp.oid = con.connamespace
                            WHERE nsp.nspname = 'public'
                              AND con.contype IN ('p', 'f', 'u', 'c')
                            ORDER BY rel.relname, con.conname
                            """
                        )
                    )
                ).fetchall()
            ]

            indexes = [
                {"table": r[0], "index": r[1], "definition": _norm_pg_expr(str(r[2]))}
                for r in (
                    await conn.execute(
                        text(
                            """
                            SELECT tablename, indexname, indexdef
                            FROM pg_indexes
                            WHERE schemaname = 'public'
                            ORDER BY tablename, indexname
                            """
                        )
                    )
                ).fetchall()
            ]

            sequences = [
                r[0]
                for r in (
                    await conn.execute(
                        text(
                            """
                            SELECT sequencename
                            FROM pg_sequences
                            WHERE schemaname = 'public'
                            ORDER BY sequencename
                            """
                        )
                    )
                ).fetchall()
            ]

            alembic_ver = None
            if "alembic_version" in tables:
                row = (
                    await conn.execute(
                        text("SELECT version_num FROM public.alembic_version LIMIT 1")
                    )
                ).fetchone()
                alembic_ver = row[0] if row else None

            row_counts: dict[str, int] = {}
            for tbl in tables:
                cnt = (
                    await conn.execute(text(f'SELECT COUNT(*) FROM public."{tbl}"'))
                ).scalar_one()
                row_counts[tbl] = int(cnt)

            data_fingerprints: dict[str, str] = {}
            if "tenant" in tables:
                t_rows = (
                    await conn.execute(text("SELECT id::text FROM public.tenant ORDER BY id"))
                ).fetchall()
                data_fingerprints["tenant_ids_sha256"] = hashlib.sha256(
                    "|".join(r[0] for r in t_rows).encode("utf-8")
                ).hexdigest()

            if "incident" in tables:
                i_rows = (
                    await conn.execute(
                        text(
                            """
                            SELECT id::text, tenant_id::text, status_projection, status_version::text
                            FROM public.incident
                            ORDER BY id
                            """
                        )
                    )
                ).fetchall()
                data_fingerprints["incident_rows_sha256"] = hashlib.sha256(
                    "|".join(":".join(r) for r in i_rows).encode("utf-8")
                ).hexdigest()

            if "effect_receipt" in tables:
                e_rows = (
                    await conn.execute(
                        text(
                            """
                            SELECT id::text, tenant_id::text, incident_id::text, state, fence::text, attempt_count::text
                            FROM public.effect_receipt
                            ORDER BY id
                            """
                        )
                    )
                ).fetchall()
                data_fingerprints["effect_receipt_rows_sha256"] = hashlib.sha256(
                    "|".join(":".join(r) for r in e_rows).encode("utf-8")
                ).hexdigest()

                state_dist = {
                    r[0]: int(r[1])
                    for r in (
                        await conn.execute(
                            text(
                                "SELECT state, COUNT(*) FROM public.effect_receipt GROUP BY state ORDER BY state"
                            )
                        )
                    ).fetchall()
                }
            else:
                state_dist = {}

            return {
                "database": db_name,
                "alembic_version": alembic_ver,
                "table_count": len(tables),
                "tables": tables,
                "column_count": len(columns),
                "constraint_count": len(constraints),
                "index_count": len(indexes),
                "sequences": sequences,
                "row_counts": row_counts,
                "effect_receipt_state_distribution": state_dist,
                "data_fingerprints": data_fingerprints,
                "_raw_schema": {
                    "tables": tables,
                    "columns": columns,
                    "constraints": constraints,
                    "indexes": indexes,
                    "sequences": sequences,
                },
                "schema_fingerprint_sha256": hashlib.sha256(
                    json.dumps(
                        {
                            "tables": tables,
                            "columns": sorted(columns, key=lambda c: (c["table"], c["column"])),
                            "constraints": constraints,
                            "indexes": indexes,
                            "sequences": sequences,
                        },
                        sort_keys=True,
                    ).encode("utf-8")
                ).hexdigest(),
            }
    finally:
        await eng.dispose()


async def _create_isolated_db(admin_eng: AsyncEngine, db_name: str) -> None:
    if db_name in PROTECTED_DBS or not db_name.startswith("opsblaze_restore_"):
        raise ValueError(f"Refusing to create non-isolated database name: {db_name}")
    async with admin_eng.connect() as conn:
        exists = (
            await conn.execute(text("SELECT 1 FROM pg_database WHERE datname = :d"), {"d": db_name})
        ).scalar_one_or_none()
        if exists is not None:
            raise RuntimeError(f"Isolated database {db_name} already exists; aborting!")
        await conn.execute(text(f'CREATE DATABASE "{db_name}"'))


async def _drop_isolated_db(admin_eng: AsyncEngine, db_name: str) -> None:
    if db_name in PROTECTED_DBS or not db_name.startswith("opsblaze_restore_"):
        raise ValueError(f"Refusing to drop protected database: {db_name}")
    async with admin_eng.connect() as conn:
        await conn.execute(
            text(
                """
                SELECT pg_terminate_backend(pid)
                FROM pg_stat_activity
                WHERE datname = :d AND pid <> pg_backend_pid()
                """
            ),
            {"d": db_name},
        )
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))


async def _upgrade_isolated_db_to_head(db_name: str) -> None:
    eng = _make_async_engine(db_name)
    try:
        async with eng.connect() as conn:

            def _run_up(sync_conn: Any) -> None:
                cfg = Config("alembic.ini")
                cfg.attributes["connection"] = sync_conn
                command.upgrade(cfg, "head")

            await conn.run_sync(_run_up)
            await conn.commit()
    finally:
        await eng.dispose()


async def run_drill() -> dict[str, Any]:
    s = get_settings()
    env = _pg_env()
    stage4_backup_dir = Path(".local/backups/stage4")
    stage4_backup_dir.mkdir(parents=True, exist_ok=True)

    # 1. Inspect existing pre-Stage-3 backups
    pre_backups: dict[str, Any] = {}
    for fname in ("opsblaze_pre_stage3.dump", "opsblaze_test_pre_stage3.dump"):
        fpath = Path(".local/backups") / fname
        toc = subprocess.run(
            [PG_RESTORE, "--list", str(fpath)],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        toc_entries = [
            line for line in toc.stdout.splitlines() if line and not line.startswith(";")
        ]
        pre_backups[fname] = {
            "path": str(fpath),
            "size_bytes": fpath.stat().st_size,
            "sha256": hashlib.sha256(fpath.read_bytes()).hexdigest(),
            "toc_entry_count": len(toc_entries),
        }

    # 2. Capture baseline fingerprints of live `opsblaze` and `opsblaze_test` BEFORE any drill
    fp_opsblaze_before = await inspect_database_fingerprint("opsblaze")
    fp_opsblaze_test_before = await inspect_database_fingerprint("opsblaze_test")

    # 3. Create new Stage 4 custom-format backups
    stage4_backups: dict[str, Any] = {}
    for db_name in ("opsblaze", "opsblaze_test"):
        out_file = stage4_backup_dir / f"{db_name}_stage4.dump"
        t0 = time.perf_counter()
        subprocess.run(
            [
                PG_DUMP,
                "-h",
                s.postgres_host,
                "-p",
                str(s.postgres_port),
                "-U",
                s.postgres_user,
                "-Fc",
                "-f",
                str(out_file),
                db_name,
            ],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        toc = subprocess.run(
            [PG_RESTORE, "--list", str(out_file)],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        toc_entries = [
            line for line in toc.stdout.splitlines() if line and not line.startswith(";")
        ]
        stage4_backups[db_name] = {
            "path": str(out_file),
            "size_bytes": out_file.stat().st_size,
            "sha256": hashlib.sha256(out_file.read_bytes()).hexdigest(),
            "toc_entry_count": len(toc_entries),
            "backup_duration_ms": elapsed_ms,
        }

    # 4. Perform isolated restore drills
    suffix = uuid.uuid4().hex[:8]
    db_pre_restore = f"opsblaze_restore_pre_s3_{suffix}"
    db_post_restore = f"opsblaze_restore_post_s3_{suffix}"
    admin_eng = _make_async_engine("postgres", autocommit=True)

    try:
        # Drill A: Restore pre-Stage-3 backup (`b3_005_claim_payload`) and forward-migrate to `stage3_002_b2_b3_e8`
        await _create_isolated_db(admin_eng, db_pre_restore)
        t0_pre = time.perf_counter()
        subprocess.run(
            [
                PG_RESTORE,
                "-h",
                s.postgres_host,
                "-p",
                str(s.postgres_port),
                "-U",
                s.postgres_user,
                "-d",
                db_pre_restore,
                "--no-owner",
                "--no-privileges",
                str(Path(".local/backups/opsblaze_test_pre_stage3.dump")),
            ],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        restore_pre_ms = round((time.perf_counter() - t0_pre) * 1000.0, 2)
        fp_pre_restored = await inspect_database_fingerprint(db_pre_restore)

        # Forward-migrate `db_pre_restore` from `b3_005_claim_payload` to `head` (`stage3_002_b2_b3_e8`)
        t0_mig = time.perf_counter()
        await _upgrade_isolated_db_to_head(db_pre_restore)
        forward_migrate_ms = round((time.perf_counter() - t0_mig) * 1000.0, 2)
        fp_pre_upgraded = await inspect_database_fingerprint(db_pre_restore)

        # Drill B: Restore current post-Stage-3 backup (`stage3_002_b2_b3_e8`)
        await _create_isolated_db(admin_eng, db_post_restore)
        t0_post = time.perf_counter()
        subprocess.run(
            [
                PG_RESTORE,
                "-h",
                s.postgres_host,
                "-p",
                str(s.postgres_port),
                "-U",
                s.postgres_user,
                "-d",
                db_post_restore,
                "--no-owner",
                "--no-privileges",
                str(stage4_backup_dir / "opsblaze_test_stage4.dump"),
            ],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        restore_post_ms = round((time.perf_counter() - t0_post) * 1000.0, 2)
        fp_post_restored = await inspect_database_fingerprint(db_post_restore)

    finally:
        await _drop_isolated_db(admin_eng, db_pre_restore)
        await _drop_isolated_db(admin_eng, db_post_restore)
        await admin_eng.dispose()

    # 5. Re-verify live `opsblaze` and `opsblaze_test` were completely untouched
    fp_opsblaze_after = await inspect_database_fingerprint("opsblaze")
    fp_opsblaze_test_after = await inspect_database_fingerprint("opsblaze_test")

    assert fp_opsblaze_before == fp_opsblaze_after, "opsblaze fingerprint changed!"
    assert fp_opsblaze_test_before == fp_opsblaze_test_after, "opsblaze_test fingerprint changed!"
    assert fp_pre_restored["alembic_version"] == "b3_005_claim_payload"
    assert fp_pre_upgraded["alembic_version"] == "stage3_002_b2_b3_e8"
    if (
        fp_pre_upgraded["schema_fingerprint_sha256"]
        != fp_opsblaze_test_before["schema_fingerprint_sha256"]
    ):
        for k in ("tables", "columns", "constraints", "indexes", "sequences"):
            v1 = fp_pre_upgraded["_raw_schema"][k]
            v2 = fp_opsblaze_test_before["_raw_schema"][k]
            if v1 != v2:
                print(f"DIFF IN {k}:")
                print("  pre_upgraded:", [x for x in v1 if x not in v2])
                print("  live_before:", [x for x in v2 if x not in v1])
        raise AssertionError(
            "Forward-migrated pre-Stage-3 schema does not match current Stage 3 schema!"
        )
    if (
        fp_post_restored["schema_fingerprint_sha256"]
        != fp_opsblaze_test_before["schema_fingerprint_sha256"]
    ):
        for k in ("tables", "columns", "constraints", "indexes", "sequences"):
            v1 = fp_post_restored["_raw_schema"][k]
            v2 = fp_opsblaze_test_before["_raw_schema"][k]
            if v1 != v2:
                print(f"POST DIFF IN {k}:")
                print("  post_restored:", [x for x in v1 if x not in v2])
                print("  live_before:", [x for x in v2 if x not in v1])
        raise AssertionError("Post-Stage-3 restored schema does not match opsblaze_test!")
    for d in (
        fp_opsblaze_before,
        fp_opsblaze_after,
        fp_opsblaze_test_before,
        fp_opsblaze_test_after,
        fp_pre_restored,
        fp_pre_upgraded,
        fp_post_restored,
    ):
        d.pop("_raw_schema", None)
    assert fp_pre_upgraded["data_fingerprints"] == fp_opsblaze_test_before["data_fingerprints"], (
        "Forward-migrated pre-Stage-3 data fingerprints do not match opsblaze_test!"
    )
    assert (
        fp_post_restored["schema_fingerprint_sha256"]
        == fp_opsblaze_test_before["schema_fingerprint_sha256"]
    ), "Post-Stage-3 restored schema does not match opsblaze_test!"
    assert fp_post_restored["data_fingerprints"] == fp_opsblaze_test_before["data_fingerprints"], (
        "Post-Stage-3 restored data fingerprints do not match opsblaze_test!"
    )

    return {
        "drill": "Stage 4 Phase 2 — PostgreSQL Backup, Isolated Restore & Forward-Migration Drill",
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "pre_stage3_backups": pre_backups,
        "stage4_backups": stage4_backups,
        "live_databases_before_and_after_identical": True,
        "opsblaze_fingerprint": fp_opsblaze_after,
        "opsblaze_test_fingerprint": fp_opsblaze_test_after,
        "isolated_restore_drill_a_pre_stage3": {
            "isolated_db_name": db_pre_restore,
            "restore_duration_ms": restore_pre_ms,
            "forward_migration_duration_ms": forward_migrate_ms,
            "restored_before_upgrade": fp_pre_restored,
            "restored_after_upgrade_to_head": fp_pre_upgraded,
            "schema_matches_live_head": (
                fp_pre_upgraded["schema_fingerprint_sha256"]
                == fp_opsblaze_test_before["schema_fingerprint_sha256"]
            ),
            "data_matches_live_head": (
                fp_pre_upgraded["data_fingerprints"] == fp_opsblaze_test_before["data_fingerprints"]
            ),
            "cleaned_up_isolated_db": True,
        },
        "isolated_restore_drill_b_post_stage3": {
            "isolated_db_name": db_post_restore,
            "restore_duration_ms": restore_post_ms,
            "restored_fingerprint": fp_post_restored,
            "schema_matches_live_head": (
                fp_post_restored["schema_fingerprint_sha256"]
                == fp_opsblaze_test_before["schema_fingerprint_sha256"]
            ),
            "data_matches_live_head": (
                fp_post_restored["data_fingerprints"]
                == fp_opsblaze_test_before["data_fingerprints"]
            ),
            "cleaned_up_isolated_db": True,
        },
    }


if __name__ == "__main__":
    result = asyncio.run(run_drill())
    out_path = Path("docs/evidence/stage4-postgres-restore-drill.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
