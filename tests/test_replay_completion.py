"""Regression tests for complete and correctly labeled synthetic histories."""

import json
from types import SimpleNamespace

import pytest

from backend.workflows.replay_completion import EXPECTED_RESULTS, validate_completion


def completed(result, encoding=b"json/plain"):
    payload = SimpleNamespace(data=json.dumps(result).encode(), metadata={"encoding": encoding})
    attrs = SimpleNamespace(result=SimpleNamespace(payloads=[payload]))
    return SimpleNamespace(
        HasField=lambda name: name == "workflow_execution_completed_event_attributes",
        workflow_execution_completed_event_attributes=attrs,
    )


@pytest.mark.parametrize("case,result", EXPECTED_RESULTS.items())
def test_valid_synthetic_completion(case, result):
    validate_completion(f"replay-{case}-fixture", [completed(result)])


@pytest.mark.parametrize("events", [[], [SimpleNamespace(HasField=lambda _: False)]])
def test_incomplete_history_is_rejected(events):
    with pytest.raises(ValueError, match="must end"):
        validate_completion("replay-approve-fixture", events)


@pytest.mark.parametrize("case,result", [
    ("approve", "CANCELLED"), ("reject", "VERIFIED"), ("cancel", "NEEDS_REVIEW"),
])
def test_wrong_scenario_outcome_is_rejected(case, result):
    with pytest.raises(ValueError, match="does not match"):
        validate_completion(f"replay-{case}-fixture", [completed(result)])


def test_nonstandard_result_encoding_is_rejected():
    with pytest.raises(ValueError, match="encoding"):
        validate_completion("replay-approve-fixture", [completed("VERIFIED", b"binary/plain")])
