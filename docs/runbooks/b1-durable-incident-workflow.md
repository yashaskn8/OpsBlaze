# Operational Runbooks — Feature B1: Durable Incident Workflow

**Owner:** Platform Reliability & Incident Orchestration Team  
**Feature Scope:** B1 — Durable Incident Workflow (Admission, Outbox, Dispatcher, Temporal Workflow, Read-Only Investigator, Status Projection)  
**Environment:** Windows-Native Development & Controlled Lab (`postgresql-x64-17` on `localhost:5432`, Temporal server on `localhost:7233`, FastAPI API on `localhost:8000`)

---

## 1. PostgreSQL Unavailable

### Symptoms
- `GET /readyz` returns `HTTP 503 Service Unavailable` with `{"status": "degraded", "database": "unavailable"}`.
- `POST /v1/incidents`, `GET /v1/incidents/{id}`, and `POST /v1/incidents/{id}/cancel` return `HTTP 503 Service Unavailable`.
- `GET /healthz` continues to return `HTTP 200 OK` (liveness is preserved so the process is not unnecessarily crash-looped during brief DB blips).
- Structured logs emit `outbox.retry` with `OperationalError` from the dispatcher loop.

### Diagnosis
1. Check the Windows PostgreSQL 17 service status and port reachability:
   ```powershell
   Get-Service -Name 'postgresql-x64-17'
   Test-NetConnection -ComputerName 'localhost' -Port 5432
   ```
2. Inspect API and dispatcher structured logs for `OperationalError` or connection timeout messages (note: credentials are automatically redacted).

### Recovery & Verification
1. If `postgresql-x64-17` is stopped, start it via Windows Services (`Start-Service -Name 'postgresql-x64-17'`).
2. Verify `GET http://localhost:8000/readyz` returns `HTTP 200 OK` (`{"status": "ready", "database": "ok"}`).
3. Verify SQLAlchemy connection pool recovery (`pool_pre_ping=True` automatically discards stale/closed connections on checkout).
4. Confirm pending Temporal activities retry automatically and project current state into `incident.status_projection`.

---

## 2. Temporal Server Unavailable

### Symptoms
- `POST /v1/incidents` **continues to succeed** with `HTTP 202 Accepted` because admission commits atomically to PostgreSQL (`incident` + `outbox`) without synchronous RPCs to Temporal.
- `GET /v1/incidents/{id}` shows `status: "DISPATCH_PENDING"` and `outbox_delivered: false`, with `outbox_attempts` incrementing and `projection_lag_ms` growing.
- Structured logs emit `outbox.retry` (and `outbox.stuck` once `attempt >= 5` or age exceeds threshold) and `/metrics` shows `dispatcher_failures_total` and `outbox_retry_total` increasing.

### Diagnosis
1. Check whether the Temporal server process is listening on `localhost:7233`:
   ```powershell
   Test-NetConnection -ComputerName 'localhost' -Port 7233
   ```
2. Inspect `/metrics` for `outbox_retry_total`, `outbox_stuck_total`, and `dispatcher_failures_total`.

### Recovery & Verification
1. Start the local persisted Temporal server:
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts/start_temporal_dev.ps1
   ```
2. The `OutboxDispatcher` automatically claims eligible rows (`available_at <= now() AND delivered_at IS NULL`) using `FOR UPDATE SKIP LOCKED` and starts the missing workflows using deterministic workflow IDs (`incident:{tenant_id}:{incident_id}`).
3. Verify `outbox_delivered: true` on `GET /v1/incidents/{id}` and confirm `status` advances to `INVESTIGATING` -> `AWAITING_APPROVAL`.

---

## 3. Dispatcher Stuck or Terminated

### Symptoms
- New incidents remain in `DISPATCH_PENDING` with `outbox_delivered: false` and `outbox_attempts: 0` (if no dispatcher is running) or `available_at` in the past (if a dispatcher crashed mid-claim).
- `GET /v1/incidents/{id}` reports `projection_stale: true` once `projection_lag_ms > 5000`.

### Diagnosis
1. Query non-destructively for pending outbox items and their lease timestamps:
   ```sql
   SELECT id, incident_id, attempt, available_at, delivered_at
   FROM outbox
   WHERE delivered_at IS NULL
   ORDER BY available_at ASC, id ASC
   LIMIT 25;
   ```
2. Check whether the `OutboxDispatcher` process/task is running.

### Recovery & Verification
1. Because outbox claims advance `available_at = now() + lease_seconds` (default 15s) inside a short transaction and release the row lock immediately before calling Temporal, a crashed dispatcher never holds a long-lived PostgreSQL lock.
2. Once `available_at <= now()`, any running or newly restarted `OutboxDispatcher` automatically reclaims the row.
3. If the prior dispatcher crashed *after* Temporal accepted `start_workflow` but *before* `delivered_at` was persisted, the reclaimed attempt receives `WorkflowAlreadyStartedError` from Temporal (due to `WorkflowIDReusePolicy.REJECT_DUPLICATE` + `WorkflowIDConflictPolicy.FAIL`), treats it as idempotent success, and sets `delivered_at = now()`.

---

## 4. Outbox Backlog

### Symptoms
- High count of rows in `outbox WHERE delivered_at IS NULL`.
- `outbox_stuck_total` counter increments in `/metrics`; `outbox.stuck` events appear in structured logs.

### Diagnosis
1. Check backlog depth and oldest pending item age:
   ```sql
   SELECT count(*) AS pending_count,
          max(EXTRACT(EPOCH FROM (now() - available_at))) AS max_lag_seconds
   FROM outbox
   WHERE delivered_at IS NULL;
   ```
2. Inspect `outbox.retry` logs to determine whether failures are caused by Temporal connectivity, task queue saturation, or database pool contention.

### Recovery & Verification
1. Verify Temporal server health and worker availability on task queue `opsblaze-b1-incident-queue`.
2. Scale or restart `OutboxDispatcher` instances (safe to run multiple concurrent dispatchers because `FOR UPDATE SKIP LOCKED` prevents double-claiming).
3. Monitor `outbox_delivered_total` in `/metrics` until `pending_count` reaches `0`.

---

## 5. Temporal Worker Crash

### Symptoms
- Outbox items are marked `outbox_delivered: true` (`workflow.started` emitted), but incidents remain in `DISPATCH_PENDING` or `INVESTIGATING` without reaching `AWAITING_APPROVAL`.
- Temporal workflow tasks or activity tasks time out waiting for a worker on `opsblaze-b1-incident-queue`.

### Diagnosis
1. Check worker process status and logs.
2. Inspect `/metrics` for `activity_retries_total`.

### Recovery & Verification
1. Restart the B1 worker (`create_b1_worker`).
2. Temporal automatically redelivers pending workflow tasks and retries unfinished activities (`b1_project_incident_status`, `b1_run_fake_investigation`) according to the bounded `RetryPolicy` (`maximum_attempts=4`).
3. Because `transition_status` in `IncidentRepository` handles idempotent same-state retries (`validate_transition` returns `False` when `current_status == target_status`), activity retries after a worker crash never corrupt `status_version` or raise false transition errors.

---

## 6. Workflow Replay Incompatibility (Non-Determinism Error)

### Symptoms
- Temporal worker logs report `NondeterminismError` when replaying an existing workflow history after a code change.
- Active workflows stall while replaying past events.

### Diagnosis
1. Run the deterministic replay verification test against captured histories:
   ```powershell
   .\.venv\Scripts\pytest.exe tests/integration/test_b1_temporal_workflow.py -k replay -v
   ```
2. Inspect changes to `backend/workflows/incident_workflow.py` for:
   - Added, removed, or reordered `workflow.execute_activity` calls without a `workflow.patched(...)` guard.
   - Direct use of `datetime.now()`, `random`, `uuid.uuid4()`, network I/O, or SQL inside `IncidentWorkflow`.

### Recovery & Verification
1. Roll back the incompatible workflow code change or gate the new branch behind `if workflow.patched("<patch-id>"):` so existing histories take the original branch while new executions take the updated branch.
2. Verify replay passes with `temporalio.worker.Replayer(workflows=[IncidentWorkflow])` before deploying the worker update.

---

## 7. Recovery After Full System Restart

### Symptoms
- Workstation or service restart stopped FastAPI, `OutboxDispatcher`, Temporal worker, Temporal dev server, and/or PostgreSQL.

### Recovery Procedure
1. **Verify PostgreSQL 17 is running:**
   ```powershell
   Get-Service -Name 'postgresql-x64-17'
   .\.venv\Scripts\alembic.exe current
   ```
   Confirm revision is `b3_005_claim_payload (head)`.
2. **Start Persisted Temporal Server:**
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts/start_temporal_dev.ps1
   ```
3. **Start B1 Worker and Outbox Dispatcher** alongside the FastAPI service (`.\.venv\Scripts\uvicorn.exe backend.api.main:app --host 127.0.0.1 --port 8000`).
4. **Post-Restart Verification:**
   - Check `GET http://127.0.0.1:8000/readyz` -> `200 OK`.
   - Confirm any pre-restart `outbox` rows with `delivered_at IS NULL` are drained by `OutboxDispatcher`.
   - Confirm any workflows paused at `AWAITING_APPROVAL` remain queryable and respond to cancellation (`POST /v1/incidents/{id}/cancel`).
