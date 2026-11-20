
from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.learning.failures import (
    FailureConflict, draft_candidate, record_case, review_classification,
)
from backend.models import (
    FailureCluster, FailureReview, ImprovementCandidate,
    OutcomeRevision, ProbeCall,
)
from backend.schemas import IncidentCreate
from backend.services import admit_incident


@pytest.fixture
async def disputed_probe(sessions_fixture):
    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "t", "case", IncidentCreate(target_id="checkout")
        )
    async with sessions_fixture() as session:
        async with session.begin():
            session.add(ProbeCall(
                id=uuid4(), incident_id=incident.id, step_no=1,
                catalog_id="shell.exec", outcome="DENIED", evidence_id=None,
            ))
            outcome = OutcomeRevision(
                id=uuid4(), tenant_id="t", incident_id=incident.id,
                effect_id=None, revision=1, label="DISPUTED",
                witness_source="OPERATOR_DISPUTE",
                observed_version=None, reason="investigation mismatch",
            )
            session.add(outcome)
        outcome_id = outcome.id
    async with sessions_fixture() as session:
        case = await record_case(session, "t", outcome_id)
    return sessions_fixture, case.id, case.cluster_id


@pytest.mark.asyncio
async def test_review_requires_durable_fact_and_cas(disputed_probe):
    sessions, case_id, old_cluster_id = disputed_probe
    async with sessions() as session:
        with pytest.raises(FailureConflict):
            await review_classification(
                session, "t", case_id, 1, "EFFECT_FAILED", None,
                "no effect receipt supports this",
            )
    async with sessions() as session:
        corrected = await review_classification(
            session, "t", case_id, 1, "PROBE_DENIED", 1,
            "denied read-only tool was recorded",
        )
        assert corrected.classification_version == 2
        assert corrected.cluster_id != old_cluster_id
    async with sessions() as session:
        with pytest.raises(FailureConflict):
            await review_classification(
                session, "t", case_id, 1, "PROBE_DENIED", 1,
                "stale reviewer decision",
            )
    async with sessions() as session:
        reviews = (await session.scalars(select(FailureReview))).all()
        assert len(reviews) == 1
        old = await session.get(FailureCluster, old_cluster_id)
        assert old.quarantined and old.case_count == 0


@pytest.mark.asyncio
async def test_reviewed_classification_allows_bounded_draft(disputed_probe):
    sessions, case_id, _ = disputed_probe
    async with sessions() as session:
        corrected = await review_classification(
            session, "t", case_id, 1, "PROBE_DENIED", 1,
            "confirmed denied probe",
        )
        cluster_id = corrected.cluster_id
    async with sessions() as session:
        candidate = await draft_candidate(
            session, "t", cluster_id,
            "CONTEXT_LIMIT", "baseline-v1", {"max_context_tokens": 256},
            "reduce stale context during reads", "LOW", "smoke-v1",
        )
        assert candidate.state == "DRAFT"
        assert candidate.tenant_id == "t"
