"""E1 restricted PostgreSQL account: real DB grants, not mocked roles."""

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.experiences import set_consent
from backend.models import ExperienceCapsule, OutboxEntry, OutcomeRevision
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
            assert (await conn.scalar(text(
                "SELECT has_table_privilege(current_user, 'public.effect_receipt', 'UPDATE')"
            ))) is False
            assert (await conn.scalar(text(
                "SELECT has_column_privilege(current_user, 'public.incident',"
                "'status_projection', 'UPDATE')"
            ))) is False

        async with main() as session:
            incident = await admit_incident(
                session, tenant, "source",
                IncidentCreate(target_id="synthetic-only"),
            )
        async with main() as session:
            await set_consent(session, tenant, incident.id, 0, True)
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
                session.add(OutboxEntry(
                    incident_id=incident.id, kind="PROJECT_EXPERIENCE",
                    payload={"outcome_id": str(outcome_id)},
                ))

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
            assert len(rows) == 2
            assert all(row.delivered_at is not None for row in rows)

        # The projector cannot fake a Temporal delivery: RLS blocks it even
        # though it has a narrow delivered_at UPDATE grant.
        async with restricted_engine.begin() as conn:
            result = await conn.execute(text(
                "UPDATE public.outbox SET delivered_at = now()"
                " WHERE id = :id RETURNING id"
            ), {"id": start_id})
            assert result.scalar_one_or_none() is None

        for sql in (
            "UPDATE public.incident SET status_projection = 'VERIFIED'"
            " WHERE tenant_id = :tenant",
            "UPDATE public.experience_consent SET allowed = false"
            " WHERE tenant_id = :tenant",
            "SELECT id FROM public.effect_receipt LIMIT 1",
            "SELECT id FROM public.incident_plan LIMIT 1",
        ):
            async with restricted_engine.connect() as conn:
                with pytest.raises(DBAPIError):
                    await conn.execute(text(sql), {"tenant": tenant})
    finally:
        await restricted_engine.dispose()
        await main_engine.dispose()
