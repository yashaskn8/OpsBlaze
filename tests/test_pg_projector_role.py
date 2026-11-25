"""E1 restricted PostgreSQL account: real DB grants, not mocked roles."""

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.experiences import set_consent, set_tenant_consent
from backend.models import (
    ExperienceCapsule, Evidence, OutboxEntry, OutcomeRevision,
)
from backend.schemas import IncidentCreate
from backend.services import admit_incident
from backend.settings import get_settings
from backend.workers.experience_projector import (
    projector_database_url, run_projector_once,
)
from backend.workers.heartbeat import record_heartbeat


@pytest.mark.integration
@pytest.mark.pg_e2e
@pytest.mark.asyncio
async def test_restricted_projector_can_capture_but_cannot_mutate_recovery():
    settings = get_settings()
    main_engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    restricted_engine = create_async_engine(
        projector_database_url(settings), pool_pre_ping=True,
    )
    main = async_sessionmaker(main_engine, expire_on_commit=False)
    projected = async_sessionmaker(restricted_engine, expire_on_commit=False)
    tenant = "projector-grants-" + uuid4().hex[:12]
    try:
        async with restricted_engine.connect() as conn:
            assert await conn.scalar(text("SELECT current_user")) == "opsblaze_projector"
            # Startup must reject privileged identities even when named correctly.
            flags = (await conn.execute(text(
                "SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb "
                "FROM pg_catalog.pg_roles WHERE rolname = current_user"
            ))).one()
            assert not any(flags)
            for table in ("effect_receipt", "incident_plan", "lineage_edge"):
                assert await conn.scalar(text(
                    "SELECT has_table_privilege(current_user, :table, 'SELECT')"
                ), {"table": f"public.{table}"}) is False
            assert (await conn.scalar(text(
                "SELECT has_table_privilege(current_user, 'public.effect_receipt', 'UPDATE')"
            ))) is False
            assert (await conn.scalar(text(
                "SELECT has_column_privilege(current_user, 'public.incident',"
                "'status_projection', 'UPDATE')"
            ))) is False
            for privilege in ("INSERT", "UPDATE", "DELETE"):
                assert await conn.scalar(text(
                    "SELECT has_table_privilege(current_user, 'public.lineage_edge', :privilege)"
                ), {"privilege": privilege}) is False
            # Allow only capture inputs, not origin actor, raw probe excerpts
            # or the adjudicator's free-form outcome rationale.
            for table, column, allowed in (
                ("incident", "target_id", True),
                ("incident", "created_by", False),
                ("incident", "request_sha256", False),
                ("outcome_revision", "label", True),
                ("outcome_revision", "reason", False),
                ("outcome_revision", "witness_source", False),
                ("evidence", "id", True),
                ("evidence", "redacted_excerpt", False),
                ("evidence", "target_id", False),
                ("evidence", "sha256", False),
            ):
                permitted = await conn.scalar(text(
                    "SELECT has_column_privilege(current_user, "
                    ":table_name, :column_name, 'SELECT')"
                ), {
                    "table_name": f"public.{table}",
                    "column_name": column,
                })
                assert permitted is allowed, (table, column)

        async with main() as session:
            incident = await admit_incident(
                session, tenant, "source",
                IncidentCreate(target_id="synthetic-only"),
            )
        async with main() as session:
            await set_consent(session, tenant, incident.id, 0, True)
        # These rows have no pending projection, including a second incident
        # of the SAME tenant: tenant filtering alone is insufficient.
        foreign_tenant = "projector-hidden-" + uuid4().hex[:12]
        async with main() as session:
            unrelated = await admit_incident(
                session, tenant, "unrelated",
                IncidentCreate(target_id="unrelated-target"),
            )
        async with main() as session:
            foreign = await admit_incident(
                session, foreign_tenant, "foreign",
                IncidentCreate(target_id="foreign-target"),
            )
        async with main() as session:
            await set_consent(session, tenant, unrelated.id, 0, True)
        async with main() as session:
            await set_tenant_consent(session, foreign_tenant, 0, True)
        foreign_outcome_id = uuid4()
        foreign_evidence_id = uuid4()
        async with main() as session:
            async with session.begin():
                session.add(OutcomeRevision(
                    id=foreign_outcome_id, tenant_id=foreign_tenant,
                    incident_id=foreign.id, revision=1, label="VERIFIED",
                    effect_id=None, witness_source="SIMULATOR_WITNESS",
                    observed_version=1, reason="unrelated confidential audit note",
                ))
                session.add(Evidence(
                    id=foreign_evidence_id, tenant_id=foreign_tenant,
                    incident_id=foreign.id, target_id="foreign-target",
                    source="SIMULATOR", source_version="1",
                    target_version=1, observed_at=datetime.now(timezone.utc),
                    sha256="a" * 64, redacted_excerpt="unrelated private source",
                    classification="INTERNAL", validity="VALID",
                ))

        outcome_id = uuid4()
        async with main() as session:
            async with session.begin():
                start = await session.scalar(select(OutboxEntry).where(
                    OutboxEntry.incident_id == incident.id,
                    OutboxEntry.kind == "START_WORKFLOW",
                ))
                start.delivered_at = datetime.now(timezone.utc)
                start_id = start.id
                session.add(OutcomeRevision(
                    id=outcome_id, tenant_id=tenant, incident_id=incident.id,
                    revision=1, label="VERIFIED", effect_id=None,
                    witness_source="SIMULATOR_WITNESS", observed_version=2,
                    reason="synthetic independent recovery outcome",
                ))
                # Pending Temporal signal must still order the later local
                # projection, even though its row is hidden by projector RLS.
                pending = OutboxEntry(
                    incident_id=incident.id, kind="PLAN_DECIDED",
                    payload={"private_signal": "never-expose-to-projector"},
                )
                session.add(pending)
                await session.flush()
                pending_id = pending.id
                session.add(OutboxEntry(
                    incident_id=incident.id, kind="PROJECT_EXPERIENCE",
                    payload={"outcome_id": str(outcome_id)},
                ))

        async with restricted_engine.connect() as conn:
            # The worker cannot read Temporal signal IDs or secret payloads.
            assert (await conn.scalar(text(
                "SELECT count(*) FROM public.outbox"
                " WHERE id IN (:start, :pending)"
            ), {"start": start_id, "pending": pending_id})) == 0
            kinds = (await conn.scalars(text(
                "SELECT kind FROM public.outbox WHERE incident_id = :incident"
            ), {"incident": incident.id})).all()
            assert kinds == ["PROJECT_EXPERIENCE"]
            # Only the incident with an outstanding projection is visible.
            assert await conn.scalar(text(
                "SELECT count(*) FROM public.incident"
                " WHERE id IN (:active, :other, :foreign)"
            ), {
                "active": incident.id, "other": unrelated.id,
                "foreign": foreign.id,
            }) == 1
            assert await conn.scalar(text(
                "SELECT count(*) FROM public.experience_consent"
                " WHERE incident_id = :other"
            ), {"other": unrelated.id}) == 0
            assert await conn.scalar(text(
                "SELECT count(*) FROM public.outcome_revision"
                " WHERE id = :id"
            ), {"id": foreign_outcome_id}) == 0
            assert await conn.scalar(text(
                "SELECT count(*) FROM public.evidence"
                " WHERE id = :id"
            ), {"id": foreign_evidence_id}) == 0
            assert await conn.scalar(text(
                "SELECT count(*) FROM public.tenant_experience_consent"
                " WHERE tenant_id = :tenant"
            ), {"tenant": foreign_tenant}) == 0
            assert await conn.scalar(text(
                "SELECT has_function_privilege(current_user,"
                " 'public.projector_intent_ready(uuid,bigint)', 'EXECUTE')"
            )) is True

        # Hidden unfinished signals must nevertheless prevent early capture.
        assert not await run_projector_once(projected, tenant_filter=tenant)
        async with main() as session:
            async with session.begin():
                prior = await session.get(OutboxEntry, pending_id)
                assert prior.delivered_at is None
                prior.delivered_at = datetime.now(timezone.utc)

        # This actually uses the separately scoped PostgreSQL identity.
        assert await run_projector_once(projected, tenant_filter=tenant)
        await record_heartbeat(projected, "experience_projector")
        async with main() as session:
            capsule = await session.scalar(select(ExperienceCapsule).where(
                ExperienceCapsule.incident_id == incident.id,
            ))
            assert capsule is not None and capsule.outcome_revision_id == outcome_id
            rows = (await session.scalars(select(OutboxEntry).where(
                OutboxEntry.incident_id == incident.id,
            ))).all()
            assert len(rows) == 3
            assert all(row.delivered_at is not None for row in rows)

        # Acknowledgment retires the source visibility grant immediately,
        # including the capsule just created by the restricted identity.
        async with restricted_engine.connect() as conn:
            for sql, param in (
                ("SELECT count(*) FROM public.incident WHERE id = :id", incident.id),
                ("SELECT count(*) FROM public.outcome_revision WHERE id = :id", outcome_id),
                ("SELECT count(*) FROM public.experience_capsule WHERE incident_id = :id", incident.id),
            ):
                assert await conn.scalar(text(sql), {"id": param}) == 0
            updated = await conn.execute(text(
                "UPDATE public.incident SET projector_lock_marker = 1"
                " WHERE id = :id RETURNING id"
            ), {"id": unrelated.id})
            assert updated.scalar_one_or_none() is None

        # The projector cannot fake a Temporal delivery: RLS blocks it even
        # though it has a narrow delivered_at UPDATE grant.
        async with restricted_engine.begin() as conn:
            result = await conn.execute(text(
                "UPDATE public.outbox SET delivered_at = now()"
                " WHERE id = :id RETURNING id"
            ), {"id": start_id})
            assert result.scalar_one_or_none() is None

        # The role must not insert a capsule for an unrelated tenant or
        # incident even when it knows the exact identifiers.
        async with restricted_engine.connect() as conn:
            with pytest.raises(DBAPIError):
                await conn.execute(text(
                    "INSERT INTO public.experience_capsule "
                    "(id, tenant_id, incident_id, outcome_revision_id,"
                    " consent_epoch, tenant_consent_epoch, target_digest,"
                    " evidence_ids, status, expires_at) "
                    "VALUES (:id, :tenant, :incident, :outcome, 1, 0,"
                    " :digest, '[]'::json, 'VALID', now() + interval '1 day')"
                ), {
                    "id": uuid4(), "tenant": foreign_tenant,
                    "incident": foreign.id, "outcome": foreign_outcome_id,
                    "digest": "0" * 64,
                })

        for sql in (
            "UPDATE public.incident SET status_projection = 'VERIFIED'"
            " WHERE tenant_id = :tenant",
            "UPDATE public.experience_consent SET allowed = false"
            " WHERE tenant_id = :tenant",
            "SELECT id FROM public.effect_receipt LIMIT 1",
            "SELECT id FROM public.incident_plan LIMIT 1",
            "SELECT created_by FROM public.incident LIMIT 1",
            "SELECT redacted_excerpt FROM public.evidence LIMIT 1",
            "SELECT reason FROM public.outcome_revision LIMIT 1",
        ):
            async with restricted_engine.connect() as conn:
                with pytest.raises(DBAPIError):
                    await conn.execute(text(sql), {"tenant": tenant})
    finally:
        await restricted_engine.dispose()
        await main_engine.dispose()
