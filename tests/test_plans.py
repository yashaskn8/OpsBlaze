
import pytest
from sqlalchemy import select, update

from backend.investigator.baseline import inspect
from backend.adapters.conformance import execute_suite
from backend.models import EffectReceipt, Evidence, Incident, IncidentPlan, OutboxEntry, ResourceEpoch, SimulatorTarget
from backend.plans import PlanConflict, decide_plan, execute_approved_simulator_plan
from backend.schemas import IncidentCreate
from backend.services import admit_incident


@pytest.fixture
async def proposed_lab(sessions_fixture):
    async with sessions_fixture() as session:
        async with session.begin():
            session.add(SimulatorTarget(tenant_id="a", target_id="checkout", version=3, healthy=False, last_fence=0))
            session.add(ResourceEpoch(tenant_id="a", target_id="checkout", latest_fence=0, version_seen=3))
    async with sessions_fixture() as session:
        incident = await admit_incident(session, "a", "plan-key", IncidentCreate(target_id="checkout"))
    assert await inspect(sessions_fixture, "a", incident.id) == "SIMULATOR_REVIEW_REQUIRED"
    async with sessions_fixture() as session:
        async with session.begin():
            await session.execute(
                update(Incident)
                .where(Incident.id == incident.id)
                .values(status_projection="AWAITING_APPROVAL", status_version=2)
            )
        plan = await session.scalar(select(IncidentPlan))
        plan_id = plan.id
    return sessions_fixture, incident.id, plan_id


@pytest.mark.asyncio
async def test_approval_is_version_bound_and_simulator_only(proposed_lab):
    sessions, incident_id, plan_id = proposed_lab
    async with sessions() as session:
        with pytest.raises(PlanConflict):
            await decide_plan(session, "a", plan_id, 10, "APPROVE")
    async with sessions() as session:
        plan = await decide_plan(session, "a", plan_id, 1, "APPROVE")
        assert plan.status == "APPROVED"
    assert await execute_approved_simulator_plan(sessions, "a", incident_id, plan_id) == "NEEDS_REVIEW"
    assert (await execute_suite(sessions)).state == "LAB_PASS"
    assert await execute_approved_simulator_plan(sessions, "a", incident_id, plan_id) == "VERIFIED"
    assert await execute_approved_simulator_plan(sessions, "a", incident_id, plan_id) == "VERIFIED"
    async with sessions() as session:
        target = await session.get(SimulatorTarget, ("a", "checkout"))
        assert target.healthy and target.version == 4
        intents = list((await session.scalars(select(OutboxEntry))).all())
        assert [row.kind for row in intents] == ["START_WORKFLOW", "PLAN_DECIDED"]


@pytest.mark.asyncio
async def test_rejection_cannot_trigger_effect(proposed_lab):
    sessions, incident_id, plan_id = proposed_lab
    async with sessions() as session:
        await decide_plan(session, "a", plan_id, 1, "REJECT")
    assert await execute_approved_simulator_plan(sessions, "a", incident_id, plan_id) == "NEEDS_REVIEW"
    async with sessions() as session:
        target = await session.get(SimulatorTarget, ("a", "checkout"))
        assert not target.healthy


@pytest.mark.asyncio
async def test_wrong_tenant_and_stale_target_rejected(proposed_lab):
    sessions, incident_id, plan_id = proposed_lab
    async with sessions() as session:
        assert await decide_plan(session, "b", plan_id, 1, "APPROVE") is None
    async with sessions() as session:
        async with session.begin():
            target = await session.get(SimulatorTarget, ("a", "checkout"))
            target.version = 9
    async with sessions() as session:
        with pytest.raises(PlanConflict):
            await decide_plan(session, "a", plan_id, 1, "APPROVE")


@pytest.mark.asyncio
async def test_approved_plan_does_not_execute_after_evidence_is_tampered(proposed_lab):
    sessions, incident_id, plan_id = proposed_lab
    async with sessions() as session:
        plan = await decide_plan(session, "a", plan_id, 1, "APPROVE")
        evidence_id = plan.evidence_ids[0]
    assert (await execute_suite(sessions)).state == "LAB_PASS"
    async with sessions() as session:
        async with session.begin():
            from uuid import UUID
            row = await session.get(Evidence, UUID(evidence_id))
            row.redacted_excerpt = '{"healthy":true}'
    assert await execute_approved_simulator_plan(sessions, "a", incident_id, plan_id) == "NEEDS_REVIEW"
    async with sessions() as session:
        target = await session.get(SimulatorTarget, ("a", "checkout"))
        assert target.healthy is False and target.version == 3
        assert (await session.scalars(select(EffectReceipt))).all() == []


@pytest.mark.asyncio
async def test_approval_rejects_modified_plan_digest(proposed_lab):
    sessions, incident_id, plan_id = proposed_lab
    async with sessions() as session:
        async with session.begin():
            plan = await session.get(IncidentPlan, plan_id)
            plan.plan_digest = "0" * 64
    async with sessions() as session:
        with pytest.raises(PlanConflict):
            await decide_plan(session, "a", plan_id, 1, "APPROVE")


@pytest.mark.asyncio
async def test_healthy_target_blocks_approval_without_version_change(proposed_lab):
    sessions, incident_id, plan_id = proposed_lab
    async with sessions() as session:
        async with session.begin():
            target = await session.get(SimulatorTarget, ("a", "checkout"))
            target.healthy = True
    async with sessions() as session:
        with pytest.raises(PlanConflict, match="evidence"):
            await decide_plan(session, "a", plan_id, 1, "APPROVE")


@pytest.mark.asyncio
async def test_rehashed_but_semantically_false_evidence_cannot_trigger_effect(proposed_lab):
    import hashlib
    from uuid import UUID

    sessions, incident_id, plan_id = proposed_lab
    async with sessions() as session:
        plan = await decide_plan(session, "a", plan_id, 1, "APPROVE")
        evidence_id = plan.evidence_ids[0]
    async with sessions() as session:
        async with session.begin():
            row = await session.get(Evidence, UUID(evidence_id))
            row.redacted_excerpt = '{"healthy":true}'
            row.sha256 = hashlib.sha256(row.redacted_excerpt.encode()).hexdigest()
    assert (await execute_suite(sessions)).state == "LAB_PASS"
    assert (
        await execute_approved_simulator_plan(sessions, "a", incident_id, plan_id)
        == "NEEDS_REVIEW"
    )
    async with sessions() as session:
        assert (await session.scalars(select(EffectReceipt))).all() == []
        target = await session.get(SimulatorTarget, ("a", "checkout"))
        assert target.healthy is False and target.version == 3


@pytest.mark.asyncio
async def test_reviewer_policy_epoch_revocation_blocks_new_effect(proposed_lab, monkeypatch):
    from backend.settings import Settings

    sessions, incident_id, plan_id = proposed_lab
    async with sessions() as session:
        approved = await decide_plan(session, "a", plan_id, 1, "APPROVE")
        assert approved.review_epoch == 1
    assert (await execute_suite(sessions)).state == "LAB_PASS"
    monkeypatch.setattr(
        "backend.plans.get_settings",
        lambda: Settings(review_epoch=2),
    )
    assert (
        await execute_approved_simulator_plan(sessions, "a", incident_id, plan_id)
        == "NEEDS_REVIEW"
    )
    async with sessions() as session:
        assert (await session.scalars(select(EffectReceipt))).all() == []
        target = await session.get(SimulatorTarget, ("a", "checkout"))
        assert target.healthy is False
