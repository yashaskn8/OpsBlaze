# OpsBlaze Runbook: Temporal Workflow Recovery & State Restoration

## 1. Context & Architecture
Temporal orchestrates durable execution of incident workflows (`IncidentWorkflow`).
In development, Temporal operates via a local dev server backed by SQLite (`.local/temporal.db` in dev; isolated sqlite databases during tests).
In production, Temporal operates against PostgreSQL or Cassandra with clustered history services.
This runbook guides operators through server crash recovery, SQLite online backup restoration, workflow history resumption, and handling of lost workflow executions.

---

## 2. Dev Server Online SQLite Backup Procedure

### 2.1 Online Non-Blocking SQLite Backup
When using SQLite-backed Temporal dev server, never copy `.db` files directly while the server is active, as WAL transactions or active journal locks can yield corrupted headers. Use Python's `sqlite3` online backup API:

```python
import sqlite3

def backup_temporal_sqlite(src_path: str, dst_path: str) -> None:
    src_conn = sqlite3.connect(src_path)
    dst_conn = sqlite3.connect(dst_path)
    try:
        with dst_conn:
            src_conn.backup(dst_conn, pages=100)
    finally:
        dst_conn.close()
        src_conn.close()
```

### 2.2 Recovering From Server Crashes
When the Temporal server process crashes:
1. Verify no orphaned `temporal.exe` processes hold file handles on `.db` or `.db-wal`:
   ```powershell
   Get-Process -Name "*temporal*" -ErrorAction SilentlyContinue | Stop-Process -Force
   ```
2. Inspect the SQLite database integrity:
   ```powershell
   sqlite3.exe .local/temporal.db "PRAGMA integrity_check;"
   ```
3. Restart Temporal dev server with the database file intact:
   ```powershell
   temporal.exe server start-dev --db-filename .local/temporal.db --port 7233
   ```
4. Restart OpsBlaze Worker processes (`backend.workflows.worker`).
5. In-flight workflows automatically resume execution from their last completed workflow decision task.

---

## 3. Resuming In-Flight Workflows
When workers reconnect to Temporal:
- Workflows blocked at `AWAITING_APPROVAL` remain paused at their `wait_condition()` checkpoint.
- Active activities in-flight during the crash will time out according to `start_to_close_timeout` (10-15s) and retry per the workflow `RetryPolicy`.
- Idempotency guards in `ActionGateway` and `DisposableTargetSimulator` prevent duplicate execution if an activity had already been dispatched before the crash.

---

## 4. Handling Lost Workflow State (Scenario B)
If Temporal history was completely wiped or restored to an empty database while PostgreSQL is intact:
1. Trigger the OpsBlaze Cross-Store Recovery Coordinator:
   ```python
   coordinator = CrossStoreRecoveryCoordinator(session_factory)
   await coordinator.run_recovery(tenant_id=tenant_id)
   ```
2. The coordinator identifies incidents with no active Temporal execution:
   - Terminal incidents (`VERIFIED`, `FAILED`, `CANCELLED`) have their outbox marked delivered.
   - Pre-execution incidents (`AWAITING_APPROVAL`) have their status reset to `DISPATCH_PENDING` and outbox undelivered, allowing `OutboxDispatcher` to restart investigation.
   - Stuck executing incidents (`EXECUTING`) without terminal receipts are quarantined to `NEEDS_REVIEW`.
