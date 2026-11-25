from types import SimpleNamespace
from uuid import uuid4

import pytest

from backend.effect_lineage import effect_relationships


@pytest.fixture
def records():
    plan = SimpleNamespace(
        id=uuid4(), tenant_id="a", incident_id=uuid4(), plan_digest="a" * 64,
        target_id="fixture", expected_version=1, action_id="SIMULATOR_MARK_HEALTHY",
        status="APPROVED", decided_by="reviewer",
    )
    effect = SimpleNamespace(
        tenant_id=plan.tenant_id, incident_id=plan.incident_id, plan_digest=plan.plan_digest,
        target_id=plan.target_id, expected_version=1, adapter_id="simulator",
        attempt_count=1, state="SENT",
    )
    grant = SimpleNamespace(
        id=uuid4(), tenant_id=plan.tenant_id, plan_id=plan.id, plan_digest=plan.plan_digest,
        scope=plan.action_id, reviewer_id=plan.decided_by, decision="APPROVE", state="CONSUMED",
    )
    return effect, plan, grant


@pytest.mark.parametrize("state", ["SENT", "UNKNOWN", "CONFIRMED", "FAILED", "NEEDS_REVIEW", "VERIFIED"])
def test_consumed_grant_is_historical_not_recovery_evidence(records, state):
    effect, plan, grant = records
    effect.state = state
    assert effect_relationships(effect, plan, grant) == ([
        ("plan", plan.id, "AUTHORIZED_BY"), ("approval", grant.id, "AUTHORIZED_BY_GRANT"),
    ], None)


def test_prepared_does_not_claim_authority(records):
    effect, plan, grant = records
    effect.state, effect.attempt_count = "PREPARED", 0
    assert effect_relationships(effect, plan, grant) == ([("plan", plan.id, "PREPARED_FOR")], None)


@pytest.mark.parametrize("field,value", [
    ("tenant_id", "b"), ("plan_digest", "b" * 64), ("target_id", "other"),
    ("expected_version", 2), ("adapter_id", "unqualified"),
])
def test_wrong_plan_binding_never_creates_link(records, field, value):
    effect, plan, grant = records
    setattr(effect, field, value)
    assert effect_relationships(effect, plan, grant) == ([], "REFERENCES_PLAN")


@pytest.mark.parametrize("field,value", [
    ("tenant_id", "b"), ("plan_id", None), ("plan_digest", "b" * 64),
    ("state", "APPROVED"), ("decision", "REJECT"), ("reviewer_id", "other"),
])
def test_invalid_grant_is_reference_only(records, field, value):
    effect, plan, grant = records
    setattr(grant, field, value)
    assert effect_relationships(effect, plan, grant) == (
        [("plan", plan.id, "REFERENCES_PLAN")], "AUTHORIZED_BY_GRANT",
    )


def test_invalid_receipt_state_is_reference_only(records):
    effect, plan, grant = records
    effect.state = "CORRUPTED"
    assert effect_relationships(effect, plan, grant) == (
        [("plan", plan.id, "REFERENCES_PLAN")], "AUTHORIZED_BY_GRANT",
    )
