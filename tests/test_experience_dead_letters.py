"""E1 terminal local projection: bounded retries, review and tenant fences."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.models import Incident, OutboxEntry
from backend.operations import queue_health
from backend.schemas import IncidentCreate
from backend.services import admit_incident
from backend.workers.dead_letters import (
    DEAD_LETTER_CODES, MAX_PROJECTOR_ATTEMPTS, DeadLetterConflict,
    acknowledge_projection_dead_letter, list_projection_dead_letters,
    mark_projection_dead_letter,
)
from backend.workers.outbox import claim_intent
from backend.workers.experience_projector import run_projector_once


async def make_lab(sessions):
    async with sessions() as session:
        incident = await admit_incident(
            session, "alpha", str(uuid4()),
            IncidentCreate(target_id="synthetic"),
        )
    async with sessions() as session:
        async with session.begin():
            start = await session.scalar(select(OutboxEntry).where(
                OutboxEntry.incident_id == incident.id,
                OutboxEntry.kind == "START_WORKFLOW",
            ))
            start.delivered_at = datetime.now(timezone.utc)
            poison = OutboxEntry(
                incident_id=incident.id, kind="PROJECT_EXPERIENCE",
                payload={"outcome_id": "not-a-uuid"},
            )
            session.add(poison)
            await session.flush()
            poison_id = poison.id
            session.add(OutboxEntry(
                incident_id=incident.id, kind="ACKNOWLEDGE_REVIEW",
            ))
    return incident.id, poison_id


@pytest.mark.asyncio
async def test_redteam_malformed_projection_dead_letters_and_unblocks_later_signal(
    sessions_fixture,
):
    sessions = sessions_fixture
    incident_id, poison_id = await make_lab(sessions)
    assert await run_projector_once(sessions)
    async with sessions() as session:
        poison = await session.get(OutboxEntry, poison_id)
        assert poison.dead_letter_code == "INVALID_PAYLOAD"
        assert poison.dead_lettered_at is not None
        assert poison.delivered_at is None
        assert poison.attempt == 1
        assert poison.dead_letter_reviewed_at is None
        snapshot = await queue_health(session, "alpha", 300)
        assert snapshot["experience_dead_letters_unreviewed"] == 1
        assert snapshot["pending_outbox"] == 1
        page = await list_projection_dead_letters(session, "alpha")
        assert len(page["records"]) == 1
        assert page["records"][0]["id"] == poison_id
        assert page["records"][0]["incident_id"] == str(incident_id)
        assert "not-a-uuid" not in str(page)

    claimed = await claim_intent(sessions)
    assert claimed is not None and claimed[3] == "ACKNOWLEDGE_REVIEW"

    async with sessions() as session:
        row = await session.get(OutboxEntry, poison_id)
        assert row.dead_lettered_at is not None


@pytest.mark.asyncio
async def test_redteam_projector_exhausts_retries_without_marking_delivered(
    sessions_fixture,
):
    sessions = sessions_fixture
    _, poison_id = await make_lab(sessions)
    async with sessions() as session:
        async with session.begin():
            poison = await session.get(OutboxEntry, poison_id)
            poison.payload = {"outcome_id": str(uuid4())}
    with patch(
        "backend.workers.experience_projector.project_experience_outbox",
        side_effect=ConnectionError("synthetic outage with secret in text"),
    ):
        for attempt in range(1, MAX_PROJECTOR_ATTEMPTS + 1):
            assert await run_projector_once(sessions)
            async with sessions() as session:
                async with session.begin():
                    poison = await session.get(OutboxEntry, poison_id)
                    assert poison.attempt == attempt
                    assert poison.delivered_at is None
                    if attempt < MAX_PROJECTOR_ATTEMPTS:
                        assert poison.dead_lettered_at is None
                        poison.available_at = datetime.now(timezone.utc) - timedelta(
                            seconds=1
                        )
                    else:
                        assert poison.dead_lettered_at is not None
                        assert poison.dead_letter_code == "RETRY_EXHAUSTED"
        # A later retry must only claim a different still-pending signal.
        next_intent = await claim_intent(sessions)
        assert next_intent is not None
        assert next_intent[0] != poison_id

    async with sessions() as session:
        page = await list_projection_dead_letters(session, "alpha")
        assert page["records"][0]["attempts"] == MAX_PROJECTOR_ATTEMPTS
        assert "secret" not in str(page)
        assert page["metadata_only"] is True


@pytest.mark.asyncio
async def test_redteam_dead_letter_review_is_one_winner_and_does_not_requeue(
    sessions_fixture,
):
    sessions = sessions_fixture
    incident_id, poison_id = await make_lab(sessions)
    assert await run_projector_once(sessions)
    async with sessions() as session:
        assert await acknowledge_projection_dead_letter(
            session, "beta", poison_id, 1, "oidc:operator",
        ) is None
    async with sessions() as session:
        with pytest.raises(DeadLetterConflict, match="version"):
            await acknowledge_projection_dead_letter(
                session, "alpha", poison_id, 2, "oidc:operator",
            )
    async with sessions() as session:
        reviewed = await acknowledge_projection_dead_letter(
            session, "alpha", poison_id, 1, "oidc:operator",
        )
        assert reviewed.dead_letter_reviewed_at is not None
        assert reviewed.dead_letter_reviewed_by == "oidc:operator"
    async with sessions() as session:
        with pytest.raises(DeadLetterConflict, match="already"):
            await acknowledge_projection_dead_letter(
                session, "alpha", poison_id, 1, "oidc:operator",
            )
    async with sessions() as session:
        row = await session.get(OutboxEntry, poison_id)
        assert row.delivered_at is None
        assert row.dead_lettered_at is not None
        assert (await list_projection_dead_letters(
            session, "alpha",
        ))["records"][0]["reviewed"] is True
        assert (await list_projection_dead_letters(
            session, "beta",
        ))["records"] == []
        metrics = await queue_health(session, "alpha", 300)
        assert metrics["experience_dead_letters_unreviewed"] == 0
        assert await session.get(Incident, incident_id) is not None


@pytest.mark.asyncio
async def test_redteam_old_worker_cannot_dead_letter_a_newer_claim(
    sessions_fixture,
):
    sessions = sessions_fixture
    _, poison_id = await make_lab(sessions)
    first = await claim_intent(sessions, domain="experience")
    assert first is not None and first[0] == poison_id
    async with sessions() as session:
        async with session.begin():
            row = await session.get(OutboxEntry, poison_id)
            row.available_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    newer = await claim_intent(sessions, domain="experience")
    assert newer is not None and newer[0] == poison_id
    assert newer[5] == first[5] + 1
    assert not await mark_projection_dead_letter(
        sessions, poison_id, "alpha", first[5], "INVALID_PAYLOAD",
    )
    assert await mark_projection_dead_letter(
        sessions, poison_id, "alpha", newer[5], "INVALID_PAYLOAD",
    )
    assert not await mark_projection_dead_letter(
        sessions, poison_id, "alpha", newer[5], "INVALID_PAYLOAD",
    )
    async with sessions() as session:
        row = await session.get(OutboxEntry, poison_id)
        assert row.dead_letter_code == "INVALID_PAYLOAD"


@pytest.mark.asyncio
async def test_redteam_dead_letter_page_keyset_and_validation(sessions_fixture):
    sessions = sessions_fixture
    _, first_id = await make_lab(sessions)
    # Isolate only the first tenant's projection, then another tenant.
    await run_projector_once(sessions)
    async with sessions() as session:
        second_incident = await admit_incident(
            session, "beta", str(uuid4()), IncidentCreate(target_id="private"),
        )
    async with sessions() as session:
        async with session.begin():
            session.add(OutboxEntry(
                incident_id=second_incident.id, kind="PROJECT_EXPERIENCE",
                payload={"outcome_id": "invalid"},
                dead_lettered_at=datetime.now(timezone.utc),
                dead_letter_code="INVALID_PAYLOAD",
                attempt=1,
            ))
    async with sessions() as session:
        page = await list_projection_dead_letters(session, "alpha", 0, 1)
        assert [row["id"] for row in page["records"]] == [first_id]
        assert page["next_cursor"] is None
        foreign = await list_projection_dead_letters(session, "beta")
        assert len(foreign["records"]) == 1
        assert foreign["records"][0]["id"] != first_id
        for limit, cursor in ((0, 0), (51, 0), (1, -1)):
            with pytest.raises(ValueError):
                await list_projection_dead_letters(
                    session, "alpha", cursor, limit,
                )
    assert DEAD_LETTER_CODES == {"INVALID_PAYLOAD", "RETRY_EXHAUSTED"}


@pytest.mark.asyncio
async def test_redteam_terminal_ledger_outage_preserves_unacknowledged_work(
    sessions_fixture, caplog,
):
    sessions = sessions_fixture
    _, poison_id = await make_lab(sessions)
    with patch(
        "backend.workers.experience_projector.mark_projection_dead_letter",
        side_effect=ConnectionError("private database DSN in exception"),
    ):
        assert await run_projector_once(sessions)

    async with sessions() as session:
        async with session.begin():
            row = await session.get(OutboxEntry, poison_id)
            assert row.delivered_at is None and row.dead_lettered_at is None
            row.available_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert "private database DSN" not in caplog.text
    assert "dead-letter ledger unavailable" in caplog.text

    # When the store is healthy again, the very same poison is terminalized.
    assert await run_projector_once(sessions)
    async with sessions() as session:
        row = await session.get(OutboxEntry, poison_id)
        assert row.dead_letter_code == "INVALID_PAYLOAD"
        assert row.attempt == 2
