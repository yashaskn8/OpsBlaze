# OpsBlaze Runbook: Outbox Reconciliation & Stuck-State Remediation

## 1. Context & Architecture
OpsBlaze utilizes a transactional transactional outbox pattern to decouple HTTP incident admission from asynchronous Temporal workflow dispatch.
- When an incident is admitted, an `outbox` row (`kind="INCIDENT_START"`) is inserted in the same PostgreSQL transaction as `tenant` and `incident`.
- The `OutboxDispatcher` (`backend.workflows.dispatcher`) polls for undelivered outbox records using `SELECT ... FOR UPDATE SKIP LOCKED`, starts the Temporal workflow, and marks `delivered_at = now()`.

---

## 2. Detecting Stuck Outbox Records
An outbox row is considered stuck if:
1. `delivered_at IS NULL` and `attempt >= max_attempts` (default 5).
2. `available_at` was more than 5 minutes ago and `attempt > 0`.
3. The associated incident is already in a terminal state (`VERIFIED`, `NEEDS_REVIEW`, `FAILED`, `CANCELLED`), but the outbox record was never marked delivered.

### Diagnostic SQL:
```sql
SELECT ob.id, ob.incident_id, inc.status_projection, ob.attempt, ob.last_error, ob.available_at
FROM outbox ob
JOIN incident inc ON ob.incident_id = inc.id
WHERE ob.delivered_at IS NULL
ORDER BY ob.available_at ASC;
```

---

## 3. Remediation Procedures

### 3.1 Stuck Outbox for Already Terminal Incidents
If the incident reached a terminal status (e.g. `VERIFIED` or `NEEDS_REVIEW` during cross-store recovery):
```sql
UPDATE outbox
SET delivered_at = NOW()
WHERE delivered_at IS NULL
  AND incident_id IN (
    SELECT id FROM incident
    WHERE status_projection IN ('VERIFIED', 'FAILED', 'NEEDS_REVIEW', 'CANCELLED')
  );
```

### 3.2 Stuck Outbox with Missing Outbox Record
If an incident is in `ACCEPTED` or `DISPATCH_PENDING` but has no corresponding row in `outbox`:
```sql
INSERT INTO outbox (incident_id, kind, attempt, available_at)
SELECT id, 'INCIDENT_START', 0, NOW()
FROM incident
WHERE status_projection IN ('ACCEPTED', 'DISPATCH_PENDING')
  AND id NOT IN (SELECT incident_id FROM outbox);
```

### 3.3 Automated Reconciliation via Coordinator
Executing Step 5 of the `CrossStoreRecoveryCoordinator` automatically performs both actions safely with transactional row locking:
```python
await coordinator.run_recovery(tenant_id=tenant_id)
```
