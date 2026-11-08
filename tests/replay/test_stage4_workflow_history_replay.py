"""Stage 4 Phase 6 — Temporal Workflow History Replay Compatibility Tests.

Verifies:
1. Pre-Stage-3 B1 workflow history replayed with Replayer without non-determinism.
2. Stage 3 approved execution history replayed with Replayer without non-determinism.
3. In-flight workflow history paused at AWAITING_APPROVAL replayed cleanly.
4. Cancelled workflow history replayed cleanly.
5. Activity retry history replayed cleanly.
6. Incomplete crash-window history replayed cleanly.
7. Replay failure detection: corrupted or non-deterministic history triggers replay failure.
"""

from __future__ import annotations

import json
from pathlib import Path

from temporalio.client import WorkflowHistory
from temporalio.worker import Replayer

from backend.workflows.incident_workflow import IncidentWorkflow

FIXTURES_DIR = Path("fixtures/workflow_histories")


def _load_history(filename: str, workflow_id: str) -> WorkflowHistory:
    fixture_path = FIXTURES_DIR / filename
    assert fixture_path.exists(), f"Missing fixture file: {fixture_path}"
    content = fixture_path.read_text(encoding="utf-8")
    return WorkflowHistory.from_json(workflow_id, content)


async def test_1_replay_pre_stage3_b1_workflow() -> None:
    """Verify that a Pre-Stage-3 B1 workflow history replays deterministically."""
    hist = _load_history("b1_workflow_v1.json", "wf-b1-v1-replay")
    assert len(hist.events) >= 30

    replayer = Replayer(workflows=[IncidentWorkflow])
    result = await replayer.replay_workflow(hist, raise_on_replay_failure=True)
    assert result.replay_failure is None


async def test_2_replay_stage3_approved_execution_workflow() -> None:
    """Verify that a Stage 3 approved execution workflow history replays deterministically."""
    hist = _load_history("stage3_execution_v1.json", "wf-stage3-v1-replay")
    assert len(hist.events) >= 50

    replayer = Replayer(workflows=[IncidentWorkflow])
    result = await replayer.replay_workflow(hist, raise_on_replay_failure=True)
    assert result.replay_failure is None


async def test_3_replay_awaiting_approval_workflow() -> None:
    """Verify that an in-flight workflow waiting at AWAITING_APPROVAL replays deterministically."""
    hist = _load_history("awaiting_approval_in_flight.json", "wf-awaiting-replay")
    assert len(hist.events) >= 20

    replayer = Replayer(workflows=[IncidentWorkflow])
    result = await replayer.replay_workflow(hist, raise_on_replay_failure=True)
    assert result.replay_failure is None


async def test_4_replay_cancelled_workflow() -> None:
    """Verify that a cancelled workflow history replays deterministically."""
    hist = _load_history("cancelled_workflow.json", "wf-cancelled-replay")
    assert len(hist.events) >= 30

    replayer = Replayer(workflows=[IncidentWorkflow])
    result = await replayer.replay_workflow(hist, raise_on_replay_failure=True)
    assert result.replay_failure is None


async def test_5_replay_activity_retry_workflow() -> None:
    """Verify that a workflow history with activity retries replays deterministically."""
    hist = _load_history("activity_retry_workflow.json", "wf-retry-replay")
    assert len(hist.events) >= 30

    replayer = Replayer(workflows=[IncidentWorkflow])
    result = await replayer.replay_workflow(hist, raise_on_replay_failure=True)
    assert result.replay_failure is None


async def test_6_replay_incomplete_crash_window_workflow() -> None:
    """Verify that an incomplete workflow history (cut during crash window) replays cleanly."""
    hist = _load_history("incomplete_crash_window.json", "wf-crash-window-replay")
    assert len(hist.events) == 15

    replayer = Replayer(workflows=[IncidentWorkflow])
    result = await replayer.replay_workflow(hist, raise_on_replay_failure=True)
    assert result.replay_failure is None


async def test_7_tampered_history_triggers_replay_failure() -> None:
    """Adversarial test: Tampered history events trigger replay failure."""
    raw_json = json.loads((FIXTURES_DIR / "b1_workflow_v1.json").read_text(encoding="utf-8"))

    # Tamper with history by removing an intermediate activity scheduled event
    # Find index of first activity scheduled event and delete it
    orig_events = raw_json.get("events", [])
    tampered_events = [ev for ev in orig_events if "activityTaskScheduledEventAttributes" not in ev]
    raw_json["events"] = tampered_events

    hist_tampered = WorkflowHistory.from_json("wf-tampered-replay", raw_json)
    replayer = Replayer(workflows=[IncidentWorkflow])

    result = await replayer.replay_workflow(hist_tampered, raise_on_replay_failure=False)
    assert result.replay_failure is not None
