"""G3 restore regression: a pending Temporal intent must remain replayable."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from backend.workers.dispatcher import workflow_id
from scripts.pg_restore_smoke import validate_pending_restore


def pending_fixture():
    tenant = "ci-pending-sentinel"
    incident_id = uuid4()
    available = datetime.now(timezone.utc).replace(microsecond=0)
    expected = {
        "outbox_id": 42,
        "incident_id": str(incident_id),
        "available_at": available.isoformat(),
        "workflow_id": workflow_id(tenant, incident_id),
    }
    found = (
        42, incident_id, "START_WORKFLOW", 2, None, None, None,
        available, expected["workflow_id"],
    )
    return found, expected


def test_pending_intent_stays_replayable_and_stable_after_restore():
    found, expected = pending_fixture()
    validate_pending_restore(found, expected)


@pytest.mark.parametrize("failure", [
    "lost_row", "changed_id", "changed_incident", "changed_kind",
    "retry_reset", "retry_increased", "boolean_retry", "premature_delivery",
    "dead_lettered", "invented_signal_payload", "missing_eligibility",
    "naive_timestamp", "changed_eligibility", "changed_workflow_id",
    "changed_expected_workflow",
])
def test_restore_sentinel_rejects_corrupt_or_prematurely_replayed_outbox(failure):
    found, expected = pending_fixture()
    values = list(found)
    if failure == "lost_row":
        found = None
    elif failure == "changed_id":
        values[0] = 43
    elif failure == "changed_incident":
        values[1] = uuid4()
    elif failure == "changed_kind":
        values[2] = "PROJECT_EXPERIENCE"
    elif failure == "retry_reset":
        values[3] = 0
    elif failure == "retry_increased":
        values[3] = 3
    elif failure == "boolean_retry":
        values[3] = True
    elif failure == "premature_delivery":
        values[4] = datetime.now(timezone.utc)
    elif failure == "dead_lettered":
        values[5] = datetime.now(timezone.utc)
    elif failure == "invented_signal_payload":
        values[6] = {"approved": True}
    elif failure == "missing_eligibility":
        values[7] = None
    elif failure == "naive_timestamp":
        values[7] = values[7].replace(tzinfo=None)
    elif failure == "changed_eligibility":
        values[7] = values[7] + timedelta(minutes=30)
    elif failure == "changed_workflow_id":
        values[8] = "opsblaze/wrong/" + expected["incident_id"]
    else:
        expected["workflow_id"] = "opsblaze/forged/" + expected["incident_id"]
    if found is not None:
        found = tuple(values)
    with pytest.raises(AssertionError, match="pending outbox replay state"):
        validate_pending_restore(found, expected)


def test_restore_check_does_not_mutate_any_record():
    row, expected = pending_fixture()
    saved = (row, dict(expected))
    validate_pending_restore(row, expected)
    assert row == saved[0]
    assert expected == saved[1]
