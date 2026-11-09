# Runbook: Durable Safety Guardrail Dispatcher & Automated Rollback

## 1. Architecture Overview

Safety guardrail evaluation is decoupled from the B1 incident state progression path:
1. When an incident transitions to terminal state (`VERIFIED`, `FAILED`, `NEEDS_REVIEW`, `CANCELLED`), `IncidentRepository.transition_status` atomically enqueues an `Outbox(kind="RELEASE_GUARDRAIL", dedupe_key="guardrail:<release_id>")` entry in PostgreSQL.
2. The `GuardrailOutboxDispatcher` background worker claims rows via `SELECT ... FOR UPDATE SKIP LOCKED` with a 15-second worker lease (`available_at = now + 15s`).
3. It invokes `ReleaseGuardrailService.evaluate_and_enforce_guardrails` to check:
   - **Class 1 (Zero-Tolerance Invariant Breaches):** N=1 trigger on unauthorized mutations, negative-control breaches, or B2 fence violations recorded in `release_event_audit`.
   - **Class 2 (Statistical Rate Metrics):** N >= 30, Candidate Error Rate > 5%, Relative Risk vs Incumbent > 1.5.
4. If breached, the release is immediately rolled back to state `ROLLED_BACK`.
5. On success or idempotent terminal state, the outbox row is marked delivered. On error, it retries with exponential backoff up to 5 attempts.

## 2. Operating the Guardrail Dispatcher

### Deployment Mode A: In-Process with API Server
Set environment variable:
```bash
ENABLE_IN_PROCESS_DISPATCHERS="true"
```
The FastAPI lifespan in `backend/api/main.py` manages startup, continuous polling (every 500ms), and graceful shutdown.

### Deployment Mode B: Standalone Worker Process
Run dedicated worker CLI:
```bash
python -m backend.releases.guardrail_dispatcher
```

## 3. Monitoring Metrics & Dashboards

Key OpenTelemetry and Prometheus metrics exposed at `/metrics`:
- `guardrail_outbox_delivered_total`: Counter of successfully delivered guardrail evaluation events.
- `guardrail_automated_rollbacks_total`: Counter of automated rollbacks triggered by guardrail breaches.
- `guardrail_outbox_dead_letter_total`: Counter of guardrail events that reached max retry attempts (5).
- `outbox_guardrail_claimed_total`: Counter of batch claims executed by the dispatcher.

## 4. Investigating Automated Rollbacks

When `guardrail_automated_rollbacks_total` fires an alert:

1. Identify the rolled-back release:
```sql
SELECT id, target_component, state, version, rollback_reason, rolled_back_at
FROM policy_release
WHERE state = 'ROLLED_BACK'
ORDER BY rolled_back_at DESC
LIMIT 5;
```

2. Inspect the audit log for breach details:
```sql
SELECT event_type, from_state, to_state, actor_id, actor_role, metadata_redacted, created_at
FROM release_event_audit
WHERE release_id = '<release_id>'
ORDER BY created_at DESC;
```

3. Query candidate vs incumbent error metrics at the time of breach:
```bash
curl -H "Authorization: Bearer <operator-token>" \
  https://api.opsblaze.corp/v1/releases/<release_id>/metrics
```

## 5. Handling Stuck Leases or Dead-Letter Outbox Rows

If an outbox worker crashes mid-evaluation:
- The lease expires automatically in 15 seconds (`available_at = claim_time + 15s`).
- Another worker automatically reclaims the row.

To inspect dead-letter or repeatedly retried guardrail events:
```sql
SELECT id, kind, attempt, available_at, last_error, payload
FROM outbox
WHERE kind = 'RELEASE_GUARDRAIL'
  AND delivered_at IS NULL
  AND attempt >= 3;
```

To manually reset a dead-letter row for reprocessing:
```sql
UPDATE outbox
SET available_at = NOW(), attempt = 0, last_error = NULL, lease_owner = NULL
WHERE id = <outbox_id>;
```
