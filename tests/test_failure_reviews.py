
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


@pytest.mark.asyncio
async def test_review_correction_invalidates_evaluated_policy_draft(disputed_probe):
    from backend.models import (
        EvaluationManifest, PolicyBundle, PolicyRollout,
    )

    sessions, case_id, cluster_id = disputed_probe
    candidate_id, manifest_id = uuid4(), uuid4()
    bundle_id, rollout_id = uuid4(), uuid4()
    async with sessions() as session:
        async with session.begin():
            session.add(ImprovementCandidate(
                id=candidate_id, tenant_id="t", source_cluster_id=cluster_id,
                kind="CONTEXT_LIMIT", base_policy_id="baseline",
                change_spec={"max_context_tokens": 256},
                hypothesis="source taxonomy may change", risk="LOW",
                evaluation_manifest_id="fixed", state="EVALUATION_READY",
                version=2, created_by="local_operator",
            ))
            session.add(EvaluationManifest(
                id=manifest_id, tenant_id="t", candidate_id=candidate_id,
                candidate_version=2, candidate_digest="a" * 64,
                incumbent_id="baseline", fixture_digest="b" * 64,
                oracle_version="fixed-v1", trial_budget=2, state="SMOKE_PASS",
            ))
            session.add(PolicyBundle(
                id=bundle_id, tenant_id="t", candidate_id=candidate_id,
                evaluation_manifest_id=manifest_id, content_digest="c" * 64,
                content={}, state="DRAFT",
            ))
            session.add(PolicyRollout(
                id=rollout_id, tenant_id="t", bundle_id=bundle_id,
                cohort_percent=1, salt="lab", state="DRAFT", version=1,
            ))
    async with sessions() as session:
        corrected = await review_classification(
            session, "t", case_id, 1, "PROBE_DENIED", 1,
            "verified denied probe means stale original classification",
        )
        assert corrected.origin_code == "PROBE_DENIED"

    async with sessions() as session:
        candidate = await session.get(ImprovementCandidate, candidate_id)
        evaluation = await session.get(EvaluationManifest, manifest_id)
        bundle = await session.get(PolicyBundle, bundle_id)
        rollout = await session.get(PolicyRollout, rollout_id)
        assert candidate.state == "INVALID"
        assert evaluation.state == "INVALID"
        assert bundle.state == "REVOKED"
        assert rollout.state == "ROLLED_BACK" and rollout.version == 2
