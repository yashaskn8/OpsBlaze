"""Validate the terminal result of each synthetic replay history."""

import json

EXPECTED_RESULTS = {"approve": "VERIFIED", "reject": "NEEDS_REVIEW", "cancel": "CANCELLED"}


def validate_completion(workflow_id, events) -> None:
    """Reject incomplete histories and mislabeled synthetic scenario outcomes."""
    case = workflow_id.split("-", 2)[1]
    if case not in EXPECTED_RESULTS:
        raise ValueError("unknown synthetic replay case")
    if not events or not events[-1].HasField(
        "workflow_execution_completed_event_attributes"
    ):
        raise ValueError("synthetic history must end in workflow completion")
    payloads = events[-1].workflow_execution_completed_event_attributes.result.payloads
    if len(payloads) != 1 or payloads[0].metadata.get("encoding") != b"json/plain":
        raise ValueError("unexpected synthetic result encoding")
    if json.loads(payloads[0].data) != EXPECTED_RESULTS[case]:
        raise ValueError("synthetic history result does not match its case")
