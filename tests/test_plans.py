
import pytest
from sqlalchemy import select, update

from backend.investigator.baseline import inspect
from backend.adapters.conformance import execute_suite
from backend.models import EffectReceipt, Evidence, Incident, IncidentPlan, OutboxEntry, ProbeCall, ResourceEpoch, SimulatorTarget
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


@pytest.mark.asyncio
async def test_incident_creator_cannot_approve_own_plan(proposed_lab):
    sessions, incident_id, plan_id = proposed_lab
    actor = "oidc:synthetic-subject-identifier"
    async with sessions() as session:
        async with session.begin():
            incident = await session.get(Incident, incident_id)
            incident.created_by = actor
    async with sessions() as session:
        with pytest.raises(PlanConflict, match="creator"):
            await decide_plan(
                session, "a", plan_id, 1, "APPROVE", decided_by=actor,
            )
    async with sessions() as session:
        plan = await session.get(IncidentPlan, plan_id)
        assert plan.status == "PROPOSED" and plan.version == 1


@pytest.mark.asyncio
async def test_approval_grant_consumed_by_first_effect_send(proposed_lab):
    from backend.models import ApprovalGrant
    from backend.plans import revoke_approval

    sessions, incident_id, plan_id = proposed_lab
    async with sessions() as session:
        approved = await decide_plan(
            session, "a", plan_id, 1, "APPROVE",
            decided_by="DEV_REVIEWER_CREDENTIAL",
            reason="checked simulator evidence and target version",
        )
        assert approved.status == "APPROVED"

    async with sessions() as session:
        grant = await session.scalar(
            select(ApprovalGrant).where(ApprovalGrant.plan_id == plan_id)
        )
        grant_id = grant.id
        assert grant.plan_digest == approved.plan_digest
        assert grant.scope == "SIMULATOR_MARK_HEALTHY"
        assert grant.state == "APPROVED" and grant.version == 1

    assert (await execute_suite(sessions)).state == "LAB_PASS"
    assert await execute_approved_simulator_plan(
        sessions, "a", incident_id, plan_id
    ) == "VERIFIED"

    async with sessions() as session:
        consumed = await session.get(ApprovalGrant, grant_id)
        assert consumed.state == "CONSUMED"
        assert consumed.consumed_at is not None
        assert consumed.version == 2
    async with sessions() as session:
        with pytest.raises(PlanConflict, match="consumed"):
            await revoke_approval(
                session, "a", grant_id, 2,
                "DEV_REVIEWER_CREDENTIAL",
                "attempt to revoke completed simulator action",
            )


@pytest.mark.asyncio
async def test_approval_revoked_before_dispatch_blocks_new_effect(proposed_lab):
    from backend.models import ApprovalGrant, ApprovalRevocation
    from backend.plans import revoke_approval

    sessions, incident_id, plan_id = proposed_lab
    async with sessions() as session:
        await decide_plan(session, "a", plan_id, 1, "APPROVE")
    async with sessions() as session:
        grant = await session.scalar(
            select(ApprovalGrant).where(ApprovalGrant.plan_id == plan_id)
        )
        grant_id = grant.id
    async with sessions() as session:
        revoked = await revoke_approval(
            session, "a", grant_id, 1, "DEV_REVIEWER_CREDENTIAL",
            "review approval withdrawn before dispatch",
        )
        assert revoked.state == "REVOKED"
    assert (await execute_suite(sessions)).state == "LAB_PASS"
    assert await execute_approved_simulator_plan(
        sessions, "a", incident_id, plan_id
    ) == "NEEDS_REVIEW"
    async with sessions() as session:
        target = await session.get(SimulatorTarget, ("a", "checkout"))
        assert target.healthy is False and target.version == 3
        assert (await session.scalars(select(EffectReceipt))).all() == []
        audit = await session.scalar(
            select(ApprovalRevocation).where(
                ApprovalRevocation.approval_id == grant_id
            )
        )
        assert audit.revoked_by == "DEV_REVIEWER_CREDENTIAL"
        assert (await session.get(IncidentPlan, plan_id)).status == "REVOKED"


@pytest.mark.asyncio
async def test_expired_grant_never_starts_a_new_effect(proposed_lab):
    from datetime import datetime, timedelta, timezone
    from backend.models import ApprovalGrant

    sessions, incident_id, plan_id = proposed_lab
    async with sessions() as session:
        await decide_plan(session, "a", plan_id, 1, "APPROVE")
    async with sessions() as session:
        async with session.begin():
            grant = await session.scalar(select(ApprovalGrant))
            grant.expires_at = datetime.now(timezone.utc) - timedelta(seconds=5)
    assert (await execute_suite(sessions)).state == "LAB_PASS"
    assert await execute_approved_simulator_plan(
        sessions, "a", incident_id, plan_id
    ) == "NEEDS_REVIEW"
    async with sessions() as session:
        assert (await session.scalars(select(EffectReceipt))).all() == []


@pytest.mark.asyncio
async def test_oidc_reviewer_rejects_legacy_incident_without_creator(proposed_lab):
    sessions, incident_id, plan_id = proposed_lab
    async with sessions() as session:
        incident = await session.get(Incident, incident_id)
        assert incident.created_by is None
    async with sessions() as session:
        with pytest.raises(PlanConflict, match="authenticated creator"):
            await decide_plan(
                session, "a", plan_id, 1, "APPROVE",
                decided_by="oidc:" + "e" * 32,
            )
    async with sessions() as session:
        plan = await session.get(IncidentPlan, plan_id)
        assert plan.status == "PROPOSED"


@pytest.mark.asyncio
async def test_approved_plan_rechecks_original_probe_evidence_links(proposed_lab):
    from uuid import UUID

    sessions, incident_id, plan_id = proposed_lab
    async with sessions() as session:
        plan = await decide_plan(session, "a", plan_id, 1, "APPROVE")
        second_evidence = UUID(plan.evidence_ids[1])
    assert (await execute_suite(sessions)).state == "LAB_PASS"

    async with sessions() as session:
        async with session.begin():
            first_probe = await session.scalar(
                select(ProbeCall).where(
                    ProbeCall.incident_id == incident_id,
                    ProbeCall.step_no == 1,
                )
            )
            first_probe.evidence_id = second_evidence

    assert (
        await execute_approved_simulator_plan(sessions, "a", incident_id, plan_id)
        == "NEEDS_REVIEW"
    )
    async with sessions() as session:
        assert (await session.scalars(select(EffectReceipt))).all() == []
        target = await session.get(SimulatorTarget, ("a", "checkout"))
        assert not target.healthy and target.version == 3


@pytest.mark.asyncio
async def test_forbidden_probe_after_approval_blocks_new_effect(proposed_lab):
    from backend.probes.gateway import invoke_probe

    sessions, incident_id, plan_id = proposed_lab
    async with sessions() as session:
        approved = await decide_plan(session, "a", plan_id, 1, "APPROVE")
        assert approved.status == "APPROVED"
    assert (await execute_suite(sessions)).state == "LAB_PASS"

    denied = await invoke_probe(
        sessions, "a", incident_id, 3, "shell.exec"
    )
    assert denied.outcome == "DENIED"
    assert await execute_approved_simulator_plan(
        sessions, "a", incident_id, plan_id
    ) == "NEEDS_REVIEW"

    async with sessions() as session:
        assert (await session.scalars(select(EffectReceipt))).all() == []
        target = await session.get(SimulatorTarget, ("a", "checkout"))
        assert not target.healthy and target.version == 3


@pytest.mark.asyncio
async def test_redteam_revoked_creator_cannot_dispatch_preapproved_effect(proposed_lab):
    from backend.models import RevokedIdentity
    from backend.security.revocation import revoke_identity

    sessions, incident_id, plan_id = proposed_lab
    creator = "oidc:" + "a" * 32
    reviewer = "oidc:" + "b" * 32
    admin = "oidc:" + "c" * 32

    async with sessions() as session:
        async with session.begin():
            incident = await session.get(Incident, incident_id)
            incident.created_by = creator
    async with sessions() as session:
        plan = await decide_plan(
            session, "a", plan_id, 1, "APPROVE", decided_by=reviewer,
        )
        assert plan.status == "APPROVED"

    async with sessions() as session:
        revoked, count = await revoke_identity(
            session, "a", creator, admin,
            "incident creator was revoked after approval",
        )
        assert revoked.actor_id == creator
        assert count == 0
    assert (await execute_suite(sessions)).state == "LAB_PASS"
    assert await execute_approved_simulator_plan(
        sessions, "a", incident_id, plan_id
    ) == "NEEDS_REVIEW"
    async with sessions() as session:
        target = await session.get(SimulatorTarget, ("a", "checkout"))
        assert target.version == 3 and not target.healthy
        assert (await session.scalars(select(EffectReceipt))).all() == []


@pytest.mark.asyncio
async def test_redteam_revoked_creator_cannot_receive_new_approval(proposed_lab):
    from backend.security.revocation import revoke_identity

    sessions, incident_id, plan_id = proposed_lab
    creator = "oidc:" + "d" * 32
    reviewer = "oidc:" + "e" * 32
    admin = "oidc:" + "f" * 32
    async with sessions() as session:
        async with session.begin():
            incident = await session.get(Incident, incident_id)
            incident.created_by = creator
    async with sessions() as session:
        await revoke_identity(
            session, "a", creator, admin,
            "incident origin identity access withdrawn",
        )
    async with sessions() as session:
        with pytest.raises(PlanConflict, match="evidence"):
            await decide_plan(
                session, "a", plan_id, 1, "APPROVE", decided_by=reviewer,
            )
    async with sessions() as session:
        plan = await session.get(IncidentPlan, plan_id)
        assert plan.status == "PROPOSED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tamper", ["adapter", "target", "version", "digest", "plan_action"],
)
async def test_redteam_receipt_plan_substitution_never_crosses_sent(
    proposed_lab, tamper,
):
    from uuid import uuid4
    from backend.effects.gateway import EffectConflict, set_state
    from backend.models import ApprovalGrant

    sessions, incident_id, plan_id = proposed_lab
    async with sessions() as session:
        approved = await decide_plan(session, "a", plan_id, 1, "APPROVE")
        digest = approved.plan_digest
        expected_version = approved.expected_version

    receipt_id = uuid4()
    async with sessions() as session:
        async with session.begin():
            session.add(EffectReceipt(
                id=receipt_id, tenant_id="a", incident_id=incident_id,
                plan_digest="0" * 64 if tamper == "digest" else digest,
                target_id="other-target" if tamper == "target" else "checkout",
                adapter_id="external" if tamper == "adapter" else "simulator",
                idempotency_key=str(receipt_id), fence=1,
                expected_version=expected_version + (1 if tamper == "version" else 0),
                state="PREPARED", attempt_count=0,
            ))
            if tamper == "plan_action":
                plan = await session.get(IncidentPlan, plan_id)
                plan.action_id = "UNAUTHORIZED_ACTION"

    with pytest.raises(EffectConflict):
        await set_state(sessions, receipt_id, "SENT", plan_id=plan_id)
    async with sessions() as session:
        receipt = await session.get(EffectReceipt, receipt_id)
        approval = await session.scalar(
            select(ApprovalGrant).where(ApprovalGrant.plan_id == plan_id)
        )
        assert receipt.state == "PREPARED"
        assert receipt.attempt_count == 0
        assert approval.state == "APPROVED"
