"""Keep admission and observation boundaries aligned on durable classifications."""

from backend import lifecycle, operations, services
from backend.workers import recovery_audit, state_age


def test_active_incident_classification_has_one_source():
    assert services.ACTIVE_STATES is lifecycle.ACTIVE_STATES
    assert operations.ACTIVE_STATES is lifecycle.ACTIVE_STATES
    assert state_age.ACTIVE_STATES is lifecycle.ACTIVE_STATES
    assert recovery_audit.ACTIVE_PROJECTIONS == frozenset(lifecycle.ACTIVE_STATES)
    assert set(lifecycle.ACTIVE_STATES) == {
        "DISPATCH_PENDING", "INVESTIGATING", "AWAITING_APPROVAL",
        "PLAN_DECIDED", "EXECUTING", "CANCEL_REQUESTED", "REVIEW_ACKNOWLEDGED",
    }


def test_unresolved_effect_classification_has_one_source():
    assert operations.UNRESOLVED_EFFECT_STATES is lifecycle.UNRESOLVED_EFFECT_STATES
    assert state_age.UNRESOLVED_EFFECT_STATES is lifecycle.UNRESOLVED_EFFECT_STATES
    assert recovery_audit.UNRESOLVED_RECEIPTS == frozenset(lifecycle.UNRESOLVED_EFFECT_STATES)
    assert set(lifecycle.UNRESOLVED_EFFECT_STATES) == {"SENT", "UNKNOWN", "CONFIRMED"}
