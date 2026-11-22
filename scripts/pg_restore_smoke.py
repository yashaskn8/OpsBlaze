"""Native, CI-only PostgreSQL restore sentinel; never use on production data.

A local backup is restored into a fresh database by the GitHub Actions job.
This checks the recovery of selected privacy/capture records and RLS schema.
It does not test Temporal persistence, multi-host failover, RPO or RTO.
"""

import argparse
import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import insert, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.engine import make_url

from backend.models import (
    ExperienceCapsule,
    ExperienceConsent,
    Incident,
    OutboxEntry,
    OutcomeRevision,
    TenantExperienceConsent,
)
from backend.settings import get_settings


def require_ephemeral_ci(phase: str) -> str:
    settings = get_settings()
    from sqlalchemy.engine import make_url

    db = make_url(settings.database_url)
    expected = (
        db.database == "opsblaze" if phase == "seed"
        else db.database is not None and db.database.startswith("opsblaze_restore_")
    )
    if (
        os.environ.get("CI") != "true"
        or settings.environment != "development"
        or db.drivername != "postgresql+asyncpg"
        or db.host not in {"127.0.0.1", "localhost"}
        or not expected
    ):
        raise SystemExit("restore sentinel is limited to ephemeral native CI databases")
    return settings.database_url


async def seed(database_url: str, manifest_path: Path) -> None:
    case_id, outcome_id, capsule_id = uuid4(), uuid4(), uuid4()
    tenant = "restore-ci-" + uuid4().hex[:12]
    now = datetime.now(timezone.utc)
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(insert(Incident).values(
                id=case_id, tenant_id=tenant, client_key="sentinel",
                request_sha256="0" * 64, target_id="synthetic-restore-only",
                created_by="ci-sentinel",
            ))
            await conn.execute(insert(TenantExperienceConsent).values(
                tenant_id=tenant, epoch=3, allowed=False,
                decided_at=now,
            ))
            await conn.execute(insert(ExperienceConsent).values(
                incident_id=case_id, tenant_id=tenant, epoch=2,
                allowed=False, decided_at=now,
            ))
            await conn.execute(insert(OutcomeRevision).values(
                id=outcome_id, tenant_id=tenant, incident_id=case_id,
                revision=1, label="VERIFIED", effect_id=None,
                witness_source="SIMULATOR_WITNESS", observed_version=1,
                reason="synthetic restore sentinel",
            ))
            await conn.execute(insert(ExperienceCapsule).values(
                id=capsule_id, tenant_id=tenant, incident_id=case_id,
                outcome_revision_id=outcome_id,
                consent_epoch=2, tenant_consent_epoch=3,
                target_digest="", evidence_ids=[], status="REVOKED",
                expires_at=now + timedelta(days=1),
            ))
            await conn.execute(insert(OutboxEntry).values(
                incident_id=case_id, kind="PROJECT_EXPERIENCE",
                delivered_at=now, payload={"outcome_id": str(outcome_id)},
            ))
            revision = await conn.scalar(text("SELECT version_num FROM alembic_version"))
        manifest_path.write_text(json.dumps({
            "tenant": tenant,
            "incident_id": str(case_id),
            "outcome_id": str(outcome_id),
            "capsule_id": str(capsule_id),
            "revision": revision,
        }), encoding="utf-8")
    finally:
        await engine.dispose()


async def verify(database_url: str, manifest_path: Path, output: Path) -> None:
    record = json.loads(manifest_path.read_text(encoding="utf-8"))
    tenant = record["tenant"]
    incident_id = UUID(record["incident_id"])
    outcome_id = UUID(record["outcome_id"])
    capsule_id = UUID(record["capsule_id"])
    head = ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
    assert record["revision"] == head, "source schema is not at migration head"
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as conn:
            version = await conn.scalar(text("SELECT version_num FROM alembic_version"))
            assert version == head, "restored migration version differs"
            incident = (await conn.execute(select(
                Incident.tenant_id, Incident.target_id,
            ).where(Incident.id == incident_id))).one_or_none()
            assert incident == (tenant, "synthetic-restore-only")
            consent = (await conn.execute(select(
                ExperienceConsent.epoch, ExperienceConsent.allowed,
            ).where(ExperienceConsent.incident_id == incident_id))).one_or_none()
            assert consent == (2, False), "incident consent revocation was lost"
            policy = (await conn.execute(select(
                TenantExperienceConsent.epoch, TenantExperienceConsent.allowed,
            ).where(TenantExperienceConsent.tenant_id == tenant))).one_or_none()
            assert policy == (3, False), "tenant consent revocation was lost"
            outcome = await conn.scalar(select(OutcomeRevision.label).where(
                OutcomeRevision.id == outcome_id,
            ))
            assert outcome == "VERIFIED"
            capsule = (await conn.execute(select(
                ExperienceCapsule.status, ExperienceCapsule.target_digest,
                ExperienceCapsule.evidence_ids,
            ).where(ExperienceCapsule.id == capsule_id))).one_or_none()
            assert capsule == ("REVOKED", "", []), "revoked capsule was rehydrated"
            completed = await conn.scalar(select(OutboxEntry.id).where(
                OutboxEntry.incident_id == incident_id,
                OutboxEntry.kind == "PROJECT_EXPERIENCE",
                OutboxEntry.delivered_at.is_not(None),
            ))
            assert completed is not None, "delivered intent was not restored"
            protected = (await conn.execute(text("""
                SELECT c.relname FROM pg_catalog.pg_class c
                JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public'
                  AND c.relname IN (
                    'outbox', 'incident', 'outcome_revision', 'evidence',
                    'experience_consent', 'tenant_experience_consent',
                    'experience_capsule'
                  )
                  AND c.relrowsecurity
            """))).scalars().all()
            assert len(protected) == 7, "RLS configuration missing in restored DB"
            # A restored RLS-enabled table without its policy or grants can
            # silently break the dedicated projector. Verify the policies.
            policy = await conn.scalar(text("""
                SELECT count(*) FROM pg_catalog.pg_policies
                WHERE schemaname = 'public' AND tablename = 'outbox'
                  AND policyname IN ('projector_outbox_read', 'projector_outbox_update')
            """))
            assert policy == 2, "restored outbox projector policies are incomplete"
            for table, column in (
                ("incident", "created_by"),
                ("outcome_revision", "reason"),
                ("evidence", "redacted_excerpt"),
            ):
                assert await conn.scalar(text("""
                    SELECT has_column_privilege(
                        'opsblaze_projector', :table_name, :column_name, 'SELECT'
                    )
                """), {
                    "table_name": f"public.{table}", "column_name": column,
                }) is False, f"restored projector can read forbidden {table}.{column}"
        # Exercise the actual restricted login against the restored database:
        # it must see the local projection but not a Temporal signal.
        projector_url = make_url(get_settings().projector_database_url)
        projector_url = projector_url.set(database=make_url(database_url).database)
        restricted = create_async_engine(projector_url)
        try:
            async with restricted.connect() as conn:
                assert await conn.scalar(text("SELECT current_user")) == "opsblaze_projector"
                count = await conn.scalar(text("""
                    SELECT count(*) FROM public.outbox
                    WHERE incident_id = :incident
                      AND kind = 'PROJECT_EXPERIENCE'
                """), {"incident": incident_id})
                assert count == 1, "restored projection is not visible to projector"
                assert await conn.scalar(text("""
                    SELECT has_column_privilege(
                        current_user, 'public.evidence', 'redacted_excerpt', 'SELECT'
                    )
                """)) is False
                assert await conn.scalar(text("""
                    SELECT has_table_privilege(
                        current_user, 'public.experience_capsule', 'INSERT'
                    )
                """)) is True, "projector capsule write grant was not restored"
                # The restored revoked incident has no pending projection;
                # RLS must not re-expose its source rows.
                for table, column, value in (
                    ("incident", "id", incident_id),
                    ("outcome_revision", "id", outcome_id),
                    ("experience_consent", "incident_id", incident_id),
                    ("tenant_experience_consent", "tenant_id", tenant),
                    ("experience_capsule", "id", capsule_id),
                ):
                    visible = await conn.scalar(text(
                        f"SELECT count(*) FROM public.{table} "
                        f"WHERE {column} = :value"
                    ), {"value": value})
                    assert visible == 0, f"restored RLS exposed revoked {table}"
            try:
                async with restricted.begin() as conn:
                    await conn.execute(text("""
                        INSERT INTO public.experience_capsule (
                            id, tenant_id, incident_id, outcome_revision_id,
                            consent_epoch, tenant_consent_epoch, target_digest,
                            evidence_ids, status, expires_at
                        ) VALUES (
                            :id, :tenant, :incident, :outcome,
                            2, 3, :digest, '[]'::json, 'VALID',
                            now() + interval '1 day'
                        )
                    """), {
                        "id": uuid4(), "tenant": tenant, "incident": incident_id,
                        "outcome": outcome_id, "digest": "0" * 64,
                    })
            except DBAPIError:
                pass  # RLS must deny a forged capsule without active intent.
            else:
                raise AssertionError("restored role can forge revoked capsule")
        finally:
            await restricted.dispose()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps({
            "result": "PASS",
            "scope": "synthetic-single-host-postgresql-only",
            "source_commit": os.environ.get("GITHUB_SHA", ""),
            "migration_revision": head,
            "revocation_rows": 2,
            "capsule_status": "REVOKED",
            "completed_projection_preserved": True,
            "rls_tables_preserved": len(protected),
            "outbox_rls_policies_preserved": policy,
            "projector_denied_source_excerpts": True,
            "projector_restored_login_checked": True,
            "revoked_source_rows_hidden_from_projector": True,
            "forged_capsule_denied_after_restore": True,
            "production_certified": False,
        }, indent=2), encoding="utf-8")
        print("Native PostgreSQL restore sentinel PASS (single-host simulator only)")
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("seed", "verify"))
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    url = require_ephemeral_ci(args.phase)
    if args.phase == "seed":
        asyncio.run(seed(url, args.manifest))
    else:
        if args.output is None:
            parser.error("--output is required for verify")
        asyncio.run(verify(url, args.manifest, args.output))


if __name__ == "__main__":
    main()
