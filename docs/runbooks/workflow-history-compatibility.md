# OpsBlaze Runbook: Workflow History Replay Compatibility & Versioning

## 1. Context & Determinism Rules
Temporal workflow definitions execute replaying against recorded history events.
Any non-deterministic change in workflow logic—such as altering the sequence of commands, adding or removing activities, or changing timers without versioning—causes `WorkflowHistoryNonDeterminismError`, corrupting in-flight workflows.

---

## 2. Temporal Versioning Rules (`workflow.patched`)

### 2.1 Rule of Precedence
1. **Never alter existing activity execution order or arguments** without wrapping new paths in `workflow.patched("patch-id")`.
2. **Never change workflow signatures or inputs**.
3. **Never introduce external I/O, threads, clocks, or random generators** into workflow code; all side effects must live inside Temporal Activities.

### 2.2 Standard Patching Pattern
```python
if workflow.patched("stage3-b2-b3-execution-v1"):
    # New logic executed for modern executions and recorded in history
    return await self._run_stage3_execution(...)
else:
    # Legacy fallback path preserved for replaying older histories
    return await self._run_legacy_b1_resolution(...)
```

---

## 3. Replay Testing with `Replayer`

### 3.1 Running Automated Replay Test Suite
Before committing any change to `IncidentWorkflow`:
```powershell
.\.venv\Scripts\pytest.exe tests/replay/test_stage4_workflow_history_replay.py -v
```

### 3.2 Canonical History Fixtures
Six canonical workflow history fixtures are maintained under `fixtures/workflow_histories/`:
1. `b1_workflow_v1.json`: Pre-Stage-3 B1 workflow history (38 events).
2. `stage3_execution_v1.json`: Stage 3 approved execution through to `VERIFIED` (64 events).
3. `awaiting_approval_in_flight.json`: In-flight workflow paused at `AWAITING_APPROVAL` (30 events).
4. `cancelled_workflow.json`: Cancelled workflow via `request_cancel` signal (41 events).
5. `activity_retry_workflow.json`: Transient activity failure with automatic retry (38 events).
6. `incomplete_crash_window.json`: Workflow interrupted mid-execution (15 events).

### 3.3 Troubleshooting Replay Failures
If `Replayer` reports a replay failure:
1. Compare the history's event sequence (`activityTaskScheduled`, `timerStarted`, `workflowTaskCompleted`) against the workflow execution order.
2. Ensure new branches added to `IncidentWorkflow.run` are guarded with `workflow.patched()`.
3. Verify that activity names registered in `Worker` match the exact string identifiers invoked by `workflow.execute_activity()`.
