"""E1 administrative redrive is single-use, source-pinned, and local only."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.experiences import set_consent, set_tenant_consent
from backend.models import (
    ExperienceCapsule, OutboxEntry, OutcomeRevision,
)
from backend.schemas import IncidentCreate
from backend.services import admit_incident
from backend.workers.dead_letters import (
    DeadLetterConflict, list_projection_dead_letters,
    redrive_projection_dead_letter,
)
from backend.workers.dispatcher import dispatch_once


async def redrive_lab(sessions, *, tenant="alpha", status="RETRY_EXHAUSTED"):
    async with sessions() as session:
        incident = await admit_incident(
            session, tenant, str(uuid4()),
            IncidentCreate(target_id="synthetic-only"),
        )
    async with sessions() as session:
        await set_consent(session, tenant, incident.id, 0, True)
    outcome_id = uuid4()
    async with sessions() as session:
        async with session.begin():
            start = await session.scalar(select(OutboxEntry).where(
                OutboxEntry.incident_id == incident.id,
                OutboxEntry.kind == "START_WORKFLOW",
            ))
            start.delivered_at = datetime.now(timezone.utc)
            session.add(OutcomeRevision(
                id=outcome_id, tenant_id=tenant, incident_id=incident.id,
                revision=1, label="VERIFIED", effect_id=None,
                witness_source="SIMULATOR_WITNESS", observed_version=2,
                reason="synthetic independent confirmation",
            ))
            source = OutboxEntry(
                incident_id=incident.id, kind="PROJECT_EXPERIENCE",
                payload={"outcome_id": str(outcome_id)}, attempt=5,
                created_at=datetime.now(timezone.utc),
                dead_lettered_at=datetime.now(timezone.utc),
                dead_letter_code=status,
                dead_letter_reviewed_at=datetime.now(timezone.utc),
                dead_letter_reviewed_by="oidc:original-operator",
            )
            session.add(source)
            await session.flush()
            source_id = source.id
    return incident.id, outcome_id, source_id


@pytest.mark.asyncio
async def test_redteam_redrive_creates_one_linked_intent_then_captures_once(
    sessions_fixture,
):
    sessions = sessions_fixture
    incident_id, outcome_id, source_id = await redrive_lab(sessions)

    async with sessions() as session:
        child = await redrive_projection_dead_letter(
            session, "alpha", source_id, 5,
            "oidc:independent-admin", "STORE_RESTORED",
        )
        child_id = child.id
    async with sessions() as session:
        original = await session.get(OutboxEntry, source_id)
        child = await session.get(OutboxEntry, child_id)
        assert original.dead_lettered_at is not None
        assert original.delivered_at is None
        assert original.redrive_source_id is None
        assert child.redrive_source_id == source_id
        assert child.redrive_authorized_by == "oidc:independent-admin"
        assert child.redrive_reason == "STORE_RESTORED"
        assert child.payload == original.payload == {"outcome_id": str(outcome_id)}
        assert child.delivered_at is None and child.dead_lettered_at is None
        page = await list_projection_dead_letters(session, "alpha")
        assert page["records"][0]["redriven_to"] == child_id

    async with sessions() as session:
        with pytest.raises(DeadLetterConflict, match="already"):
            await redrive_projection_dead_letter(
                session, "alpha", source_id, 5,
                "oidc:another-admin", "TRANSIENT_FIX_VERIFIED",
            )

    temporal_client = AsyncMock()
    assert await dispatch_once(temporal_client, sessions)
    temporal_client.start_workflow.assert_not_called()
    temporal_client.get_workflow_handle.assert_not_called()
    async with sessions() as session:
        child = await session.get(OutboxEntry, child_id)
        assert child.delivered_at is not None
        assert child.dead_lettered_at is None
        capsule = await session.scalar(
            select(ExperienceCapsule).where(
                ExperienceCapsule.incident_id == incident_id,
                ExperienceCapsule.outcome_revision_id == outcome_id,
            )
        )
        assert capsule is not None and capsule.status == "VALID"
        assert capsule.target_digest
        assert (await session.get(OutboxEntry, source_id)).delivered_at is None


@pytest.mark.asyncio
async def test_redteam_redrive_needs_review_distinct_admin_and_exact_attempt(
    sessions_fixture,
):
    sessions = sessions_fixture
    _, _, source_id = await redrive_lab(sessions)
    async with sessions() as session:
        assert await redrive_projection_dead_letter(
            session, "other", source_id, 5,
            "oidc:admin", "STORE_RESTORED",
        ) is None
    for attempts, actor, error in (
        (4, "oidc:admin", "version"),
        (5, "oidc:original-operator", "independent"),
    ):
        async with sessions() as session:
            with pytest.raises(DeadLetterConflict, match=error):
                await redrive_projection_dead_letter(
                    session, "alpha", source_id, attempts,
                    actor, "STORE_RESTORED",
                )
    async with sessions() as session:
        async with session.begin():
            (await session.get(
                OutboxEntry, source_id,
            )).dead_letter_reviewed_at = None
    async with sessions() as session:
        with pytest.raises(DeadLetterConflict, match="reviewed"):
            await redrive_projection_dead_letter(
                session, "alpha", source_id, 5,
                "oidc:admin", "STORE_RESTORED",
            )
    async with sessions() as session:
        with pytest.raises(DeadLetterConflict, match="unsupported"):
            await redrive_projection_dead_letter(
                session, "alpha", source_id, 5,
                "oidc:admin", "UNSAFE_OVERRIDE",
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["INVALID_PAYLOAD", None])
async def test_redteam_malformed_or_nonterminal_source_cannot_redrive(
    sessions_fixture, code,
):
    sessions = sessions_fixture
    _, _, source_id = await redrive_lab(sessions, status=code)
    async with sessions() as session:
        with pytest.raises(DeadLetterConflict, match="only reviewed"):
            await redrive_projection_dead_letter(
                session, "alpha", source_id, 5,
                "oidc:admin", "STORE_RESTORED",
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", [
    "DISPUTE", "CONSENT_REVOKED", "CONSENT_REFRESHED", "TENANT_REVOKED",
    "EXPIRED_SOURCE", "ALREADY_CAPTURED", "ALTERED_PAYLOAD",
])
async def test_redteam_redrive_never_resurrects_stale_memory(
    sessions_fixture, mutation,
):
    sessions = sessions_fixture
    incident_id, outcome_id, source_id = await redrive_lab(sessions)
    if mutation == "CONSENT_REVOKED":
        async with sessions() as session:
            await set_consent(session, "alpha", incident_id, 1, False)
    elif mutation == "CONSENT_REFRESHED":
        async with sessions() as session:
            await set_consent(session, "alpha", incident_id, 1, True)
    elif mutation == "TENANT_REVOKED":
        async with sessions() as session:
            await set_tenant_consent(session, "alpha", 0, False)
    elif mutation == "DISPUTE":
        async with sessions() as session:
            async with session.begin():
                session.add(OutcomeRevision(
                    id=uuid4(), tenant_id="alpha", incident_id=incident_id,
                    revision=2, label="DISPUTED", effect_id=None,
                    witness_source="OPERATOR_DISPUTE", observed_version=2,
                    reason="independent synthetic reversal",
                ))
    elif mutation == "EXPIRED_SOURCE":
        async with sessions() as session:
            async with session.begin():
                source = await session.get(OutboxEntry, source_id)
                source.created_at = datetime.now(timezone.utc) - timedelta(days=31)
    elif mutation == "ALREADY_CAPTURED":
        from backend.experiences import capture_capsule

        async with sessions() as session:
            assert (await capture_capsule(
                session, "alpha", incident_id,
            )).outcome_revision_id == outcome_id
    elif mutation == "ALTERED_PAYLOAD":
        async with sessions() as session:
            async with session.begin():
                (await session.get(
                    OutboxEntry, source_id,
                )).payload = {"outcome_id": "not-a-uuid"}

    async with sessions() as session:
        with pytest.raises(DeadLetterConflict):
            await redrive_projection_dead_letter(
                session, "alpha", source_id, 5,
                "oidc:admin", "STORE_RESTORED",
            )
    async with sessions() as session:
        rows = (await session.scalars(
            select(OutboxEntry).where(
                OutboxEntry.redrive_source_id == source_id
            )
        )).all()
        assert rows == []


@pytest.mark.asyncio
async def test_redteam_redrive_child_cannot_form_unlimited_redrive_chain(
    sessions_fixture,
):
    sessions = sessions_fixture
    _, _, source_id = await redrive_lab(sessions)
    async with sessions() as session:
        child = await redrive_projection_dead_letter(
            session, "alpha", source_id, 5, "oidc:admin", "STORE_RESTORED",
        )
        child_id = child.id
    async with sessions() as session:
        async with session.begin():
            child = await session.get(OutboxEntry, child_id)
            child.dead_lettered_at = datetime.now(timezone.utc)
            child.dead_letter_code = "RETRY_EXHAUSTED"
            child.dead_letter_reviewed_at = datetime.now(timezone.utc)
            child.dead_letter_reviewed_by = "oidc:operator-two"
            child.attempt = 5
    async with sessions() as session:
        with pytest.raises(DeadLetterConflict, match="original"):
            await redrive_projection_dead_letter(
                session, "alpha", child_id, 5, "oidc:another-admin",
                "STORE_RESTORED",
            )
