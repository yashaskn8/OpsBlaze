"""Historical effect links; never authorize or execute an action."""

EFFECT_RELATIONS = frozenset({
    "PREPARED_FOR", "REFERENCES_PLAN", "AUTHORIZED_BY", "AUTHORIZED_BY_GRANT",
})


def matching_effect_plan(effect, plan) -> bool:
    return (
        plan is not None
        and plan.tenant_id == effect.tenant_id
        and plan.incident_id == effect.incident_id
        and plan.plan_digest == effect.plan_digest
        and plan.target_id == effect.target_id
        and plan.expected_version == effect.expected_version
        and plan.action_id == "SIMULATOR_MARK_HEALTHY"
        and effect.adapter_id == "simulator"
    )


def consumed_effect_authority(effect, plan, grant) -> bool:
    return (
        effect.attempt_count == 1
        and effect.state in {"SENT", "UNKNOWN", "CONFIRMED", "FAILED", "NEEDS_REVIEW", "VERIFIED"}
        and grant is not None
        and grant.tenant_id == plan.tenant_id
        and grant.plan_id == plan.id
        and grant.plan_digest == plan.plan_digest
        and grant.scope == plan.action_id
        and grant.decision == "APPROVE"
        and grant.state == "CONSUMED"
        and plan.status == "APPROVED"
        and plan.decided_by == grant.reviewer_id
    )


def effect_relationships(effect, plan, grant):
    """Return typed historical links and an explicit unresolved relation, if any."""
    if not matching_effect_plan(effect, plan):
        return [], "REFERENCES_PLAN"
    if effect.state == "PREPARED" and effect.attempt_count == 0:
        return [("plan", plan.id, "PREPARED_FOR")], None
    if not consumed_effect_authority(effect, plan, grant):
        return [("plan", plan.id, "REFERENCES_PLAN")], "AUTHORIZED_BY_GRANT"
    return [
        ("plan", plan.id, "AUTHORIZED_BY"),
        ("approval", grant.id, "AUTHORIZED_BY_GRANT"),
    ], None
