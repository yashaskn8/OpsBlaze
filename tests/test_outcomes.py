
import pytest
from sqlalchemy import select

from backend.adapters.simulator import SimulatorAdapter
from backend.effects.gateway import execute_simulator_effect
from backend.effects.reservations import reserve_simulator_target
from backend.models import (
    EffectReceipt, OutcomeRevision, ResourceEpoch, SimulatorTarget
)
from backend.outcomes import OutcomeConflict, adjudicate_simulator, dispute_outcome
from backend.schemas import IncidentCreate
from backend.services import admit_incident


@pytest.fixture
async def witnessed_effect(sessions_fixture):
    async with sessions_fixture() as session:
        async with session.begin():
            session.add(SimulatorTarget(tenant_id="a", target_id="checkout", version=1, healthy=False, last_fence=0))
            session.add(ResourceEpoch(tenant_id="a", target_id="checkout", latest_fence=0, version_seen=1))
    async with sessions_fixture() as session:
        incident = await admit_incident(session, "a", "outcome", IncidentCreate(target_id="checkout"))
    permit = await reserve_simulator_target(sessions_fixture, "a", "checkout", incident.id, "a" * 64)
    adapter = SimulatorAdapter(sessions_fixture)
    assert await execute_simulator_effect(sessions_fixture, adapter, permit) == "VERIFIED"
    async with sessions_fixture() as session:
        receipt = await session.scalar(select(EffectReceipt))
    return sessions_fixture, adapter, incident.id, receipt.id


@pytest.mark.asyncio
async def test_independent_witness_and_dispute_revisions(witnessed_effect):
    sessions, adapter, incident_id, effect_id = witnessed_effect
    assert await adjudicate_simulator(sessions, adapter, "a", incident_id, effect_id) == "VERIFIED"
    assert await adjudicate_simulator(sessions, adapter, "a", incident_id, effect_id) == "VERIFIED"
    async with sessions() as session:
        outcomes = list((await session.scalars(select(OutcomeRevision))).all())
        assert len(outcomes) == 1
        first = outcomes[0]
    async with sessions() as session:
        disputed = await dispute_outcome(session, "a", first.id, 1, "independent check disagrees")
        assert disputed.label == "DISPUTED" and disputed.revision == 2
    assert await adjudicate_simulator(sessions, adapter, "a", incident_id, effect_id) == "DISPUTED"
    async with sessions() as session:
        assert len(list((await session.scalars(select(OutcomeRevision))).all())) == 2


@pytest.mark.asyncio
async def test_stale_revision_and_tenant_scope(witnessed_effect):
    sessions, adapter, incident_id, effect_id = witnessed_effect
    await adjudicate_simulator(sessions, adapter, "a", incident_id, effect_id)
    async with sessions() as session:
        first = await session.scalar(select(OutcomeRevision))
    async with sessions() as session:
        assert await dispute_outcome(session, "b", first.id, 1, "tenant mismatch") is None
    async with sessions() as session:
        with pytest.raises(OutcomeConflict):
            await dispute_outcome(session, "a", first.id, 5, "stale revision")


@pytest.mark.asyncio
async def test_disputing_verified_outcome_quarantines_capsules_and_lessons(witnessed_effect):
    from backend.experiences import capture_capsule, list_capsules, set_consent
    from backend.memory.lessons import create_draft, validate_draft
    from backend.models import ExperienceCapsule, OperationalLesson

    sessions, adapter, incident_id, effect_id = witnessed_effect
    assert await adjudicate_simulator(sessions, adapter, "a", incident_id, effect_id) == "VERIFIED"
    async with sessions() as session:
        await set_consent(session, "a", incident_id, 0, True)
    async with sessions() as session:
        capsule = await capture_capsule(session, "a", incident_id)
    async with sessions() as session:
        lesson = await create_draft(session, "a", capsule.id, "CHECK_VERSION")
    async with sessions() as session:
        await validate_draft(session, "a", lesson.id, 1)
    async with sessions() as session:
        assert len(await list_capsules(session, "a")) == 1
        first = await session.scalar(select(OutcomeRevision))
    async with sessions() as session:
        await dispute_outcome(
            session, "a", first.id, 1, "witness was later independently disputed"
        )
    async with sessions() as session:
        assert await list_capsules(session, "a") == []
        capsule_now = await session.get(ExperienceCapsule, capsule.id)
        lesson_now = await session.get(OperationalLesson, lesson.id)
        assert capsule_now.status == "QUARANTINED"
        assert lesson_now.status == "REVOKED"
        assert lesson_now.version == 3


@pytest.mark.asyncio
async def test_outcome_reversal_invalidates_dependent_policy_lineage(witnessed_effect):
    from uuid import uuid4
    from backend.models import (
        EvaluationManifest, FailureCase, FailureCluster, ImprovementCandidate,
        PolicyBundle, PolicyRollout
    )

    sessions, adapter, incident_id, effect_id = witnessed_effect
    assert await adjudicate_simulator(sessions, adapter, "a", incident_id, effect_id) == "VERIFIED"

    cluster_id, candidate_id, eval_id = uuid4(), uuid4(), uuid4()
    bundle_id, rollout_id = uuid4(), uuid4()
    async with sessions() as session:
        async with session.begin():
            first = await session.scalar(select(OutcomeRevision))
            session.add(FailureCluster(
                id=cluster_id, tenant_id="a", signature="test", version=1,
                case_count=1, quarantined=False
            ))
            session.add(FailureCase(
                id=uuid4(), tenant_id="a", incident_id=incident_id,
                outcome_revision_id=first.id, cluster_id=cluster_id,
                origin_code="EFFECT_FAILED", first_bad_step=None,
                classification_version=1, review_status="CLASSIFIED",
            ))
            session.add(ImprovementCandidate(
                id=candidate_id, tenant_id="a", source_cluster_id=cluster_id,
                kind="CONTEXT_LIMIT", base_policy_id="pinned-v1",
                change_spec={"max_context_tokens": 256},
                hypothesis="reduce stale evidence", risk="LOW",
                evaluation_manifest_id="fixture-v1", state="EVALUATION_READY",
                version=2, created_by="local_operator",
            ))
            session.add(EvaluationManifest(
                id=eval_id, tenant_id="a", candidate_id=candidate_id,
                candidate_version=2, candidate_digest="0" * 64,
                incumbent_id="pinned-v1", fixture_digest="f" * 64,
                oracle_version="fixed-v1", trial_budget=2, state="SMOKE_PASS",
            ))
            session.add(PolicyBundle(
                id=bundle_id, tenant_id="a", candidate_id=candidate_id,
                evaluation_manifest_id=eval_id, content_digest="c" * 64,
                content={}, state="DRAFT",
            ))
            session.add(PolicyRollout(
                id=rollout_id, tenant_id="a", bundle_id=bundle_id,
                cohort_percent=1, salt="sandbox", state="DRAFT", version=1,
            ))
        outcome_id = first.id
    async with sessions() as session:
        await dispute_outcome(session, "a", outcome_id, 1, "oracle no longer supports result")
    async with sessions() as session:
        assert (await session.get(FailureCluster, cluster_id)).quarantined
        assert (await session.get(ImprovementCandidate, candidate_id)).state == "INVALID"
        assert (await session.get(EvaluationManifest, eval_id)).state == "INVALID"
        assert (await session.get(PolicyBundle, bundle_id)).state == "REVOKED"
        rollout = await session.get(PolicyRollout, rollout_id)
        assert rollout.state == "ROLLED_BACK" and rollout.version == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("fake", [
    (2.0, True),
    (2, 1),
    ("2", True),
    (2,),
])
async def test_redteam_untyped_independent_outcome_cannot_certify_recovery(
    witnessed_effect, fake,
):
    sessions, adapter, incident_id, effect_id = witnessed_effect

    class InvalidWitness(SimulatorAdapter):
        async def observe(self, tenant_id, target_id):
            return fake

    assert await adjudicate_simulator(
        sessions, InvalidWitness(sessions), "a", incident_id, effect_id,
    ) == "DISPUTED"
    async with sessions() as session:
        rows = (await session.scalars(select(OutcomeRevision))).all()
        assert len(rows) == 1 and rows[0].label == "DISPUTED"
        if len(fake) == 2 and type(fake[0]) is int:
            assert rows[0].observed_version == fake[0]
        else:
            assert rows[0].observed_version is None
