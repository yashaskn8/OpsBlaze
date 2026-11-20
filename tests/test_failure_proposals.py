
from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.learning.failures import (
    FailureConflict, classify, draft_candidate, record_case,
    submit_candidate, validate_change,
)
from backend.models import (
    EffectReceipt, FailureCase, FailureCluster, ImprovementCandidate,
    OutcomeRevision,
)
from backend.schemas import IncidentCreate
from backend.services import admit_incident


@pytest.fixture
async def failure_case(sessions_fixture):
    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "t", "failure", IncidentCreate(target_id="checkout")
        )
    async with sessions_fixture() as session:
        async with session.begin():
            outcome = OutcomeRevision(
                id=uuid4(), tenant_id="t", incident_id=incident.id,
                effect_id=None, revision=1, label="DISPUTED",
                witness_source="OPERATOR_DISPUTE",
                observed_version=None, reason="reviewed discrepancy",
            )
            session.add(outcome)
        outcome_id = outcome.id
    async with sessions_fixture() as session:
        case = await record_case(session, "t", outcome_id)
    return sessions_fixture, incident.id, outcome_id, case


@pytest.mark.asyncio
async def test_dispute_classifies_as_uncertain_and_is_idempotent(failure_case):
    sessions, incident_id, outcome_id, first = failure_case
    assert first.origin_code == "OUTCOME_DISPUTED"
    assert first.review_status == "UNCERTAIN"
    async with sessions() as session:
        duplicate = await record_case(session, "t", outcome_id)
        assert duplicate.id == first.id
    async with sessions() as session:
        cluster = await session.get(FailureCluster, first.cluster_id)
        assert cluster.case_count == 1
        assert len((await session.scalars(select(FailureCase))).all()) == 1


def test_change_validator_rejects_permission_or_code_edits():
    for kind, spec in (
        ("PROBE_ORDER", {"order": ["shell.exec"]}),
        ("CONTEXT_LIMIT", {"max_context_tokens": 1000}),
        ("ROUTE_SWITCH", {"strategy": "autonomous", "permissions": ["admin"]}),
        ("MIGRATION", {"sql": "ALTER TABLE incident"}),
    ):
        with pytest.raises(FailureConflict):
            validate_change(kind, spec)
    validate_change("PROBE_ORDER", {"order": ["simulator.health"]})


@pytest.mark.asyncio
async def test_uncertain_cluster_cannot_submit_unreviewed_proposal(failure_case):
    sessions, _, _, case = failure_case
    async with sessions() as session:
        with pytest.raises(FailureConflict):
            await draft_candidate(
                session, "t", case.cluster_id, "CONTEXT_LIMIT",
                "baseline-v1", {"max_context_tokens": 256},
                "reduce irrelevant evidence", "LOW", "fixture-v1",
            )
