
from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.learning.failures import (
    FailureConflict, draft_candidate, record_case, validate_change,
)
from backend.models import (
    FailureCase, FailureCluster, OutcomeRevision,
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
        ("PROBE_ORDER", {"order": [{"tool": "simulator.health"}]}),
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


@pytest.mark.asyncio
async def test_old_outcome_revision_cannot_create_failure_case(sessions_fixture):
    from uuid import uuid4
    from backend.learning.failures import FailureConflict, record_case
    from backend.models import OutcomeRevision
    from backend.schemas import IncidentCreate
    from backend.services import admit_incident

    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "t", "revision", IncidentCreate(target_id="checkout")
        )
    earlier, later = uuid4(), uuid4()
    async with sessions_fixture() as session:
        async with session.begin():
            session.add_all([
                OutcomeRevision(
                    id=earlier, tenant_id="t", incident_id=incident.id,
                    effect_id=None, revision=1, label="DISPUTED",
                    witness_source="OPERATOR_DISPUTE", observed_version=None,
                    reason="original uncertain label",
                ),
                OutcomeRevision(
                    id=later, tenant_id="t", incident_id=incident.id,
                    effect_id=None, revision=2, label="DISPUTED",
                    witness_source="OPERATOR_DISPUTE", observed_version=None,
                    reason="newer independent check",
                ),
            ])
    async with sessions_fixture() as session:
        with pytest.raises(FailureConflict, match="superseded"):
            await record_case(session, "t", earlier)
    async with sessions_fixture() as session:
        current = await record_case(session, "t", later)
        assert current.outcome_revision_id == later


@pytest.mark.asyncio
async def test_candidate_submission_rechecks_latest_source_revision(sessions_fixture):
    from uuid import uuid4
    from backend.learning.failures import (
        FailureConflict, record_case, draft_candidate, submit_candidate,
    )
    from backend.models import FailureCase, OutcomeRevision
    from backend.schemas import IncidentCreate
    from backend.services import admit_incident

    async with sessions_fixture() as session:
        incident = await admit_incident(
            session, "t", "candidate", IncidentCreate(target_id="checkout")
        )
    first_outcome = uuid4()
    async with sessions_fixture() as session:
        async with session.begin():
            session.add(OutcomeRevision(
                id=first_outcome, tenant_id="t", incident_id=incident.id,
                effect_id=None, revision=1, label="DISPUTED",
                witness_source="OPERATOR_DISPUTE", observed_version=None,
                reason="reported uncertain effect",
            ))
    async with sessions_fixture() as session:
        case = await record_case(session, "t", first_outcome)
    async with sessions_fixture() as session:
        async with session.begin():
            source = await session.get(FailureCase, case.id)
            source.review_status = "CLASSIFIED"

    async with sessions_fixture() as session:
        candidate = await draft_candidate(
            session, "t", case.cluster_id,
            "CONTEXT_LIMIT", "baseline-v1", {"max_context_tokens": 256},
            "safe context window for simulator evidence", "LOW", "fixed-v1",
        )

    async with sessions_fixture() as session:
        async with session.begin():
            session.add(OutcomeRevision(
                id=uuid4(), tenant_id="t", incident_id=incident.id,
                effect_id=None, revision=2, label="DISPUTED",
                witness_source="OPERATOR_DISPUTE", observed_version=None,
                reason="revised outcome after draft",
            ))
    async with sessions_fixture() as session:
        with pytest.raises(FailureConflict, match="superseded"):
            await submit_candidate(session, "t", candidate.id, 1)


@pytest.mark.asyncio
async def test_failure_lineage_is_transactional_and_reconstructable(failure_case):
    from sqlalchemy import delete
    from backend.lineage import build_lineage, read_lineage_page
    from backend.lineage_projection import rebuild_evidence_page
    from backend.models import LineageEdge

    sessions, incident_id, outcome_id, case = failure_case
    async with sessions() as session:
        row = await session.scalar(select(LineageEdge).where(LineageEdge.from_type == "failure"))
        assert (row.from_id, row.to_id, row.relation) == (case.id, outcome_id, "CLASSIFIES")
        assert (await build_lineage(session, "t", incident_id))["evidence_projection"] == "MATCHED"
    async with sessions() as session:
        async with session.begin():
            await session.execute(delete(LineageEdge).where(LineageEdge.from_type == "failure"))
    async with sessions() as session:
        assert (await read_lineage_page(session, "t", incident_id, "failure"))["evidence_projection"] == "STALE_OR_MISSING"
    async with sessions() as session:
        async with session.begin():
            assert await rebuild_evidence_page(session, "other", incident_id, "failure") is None
            assert (await rebuild_evidence_page(session, "t", incident_id, "failure"))["processed"] == 1
    async with sessions() as session:
        assert (await build_lineage(session, "t", incident_id))["evidence_projection"] == "MATCHED"
