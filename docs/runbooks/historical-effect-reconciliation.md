# OpsBlaze Runbook: Historical Effect Receipt Safety & Quarantine

## 1. Context & Invariants
During Stage 4 investigation, 29 historical effect receipt rows were identified in `opsblaze_test`:
- States: `FAILED: 2, NEEDS_REVIEW: 1, PREPARED: 3, SENT: 13, VERIFIED: 10`.
- Characteristics: Legacy pre-Stage-3 schema representation (lacked `claim_token` / set to `NULL`, empty `adapter_digest`, associated with older simulated adapters).
- Invariants:
  1. **Zero Discard Rule:** Never drop, truncate, or overwrite historical receipt rows.
  2. **Fail-Closed Quarantine:** If a historical receipt in `SENT` or `UNKNOWN` points to an unregistered adapter, an expired simulator, or returns `NOT_FOUND` on query, it MUST be quarantined to `NEEDS_REVIEW`.
  3. **No Autonomous Mutation:** Never re-dispatch or re-execute external mutations for historical receipts without human incident commander approval.

---

## 2. Reconciling Historical Receipts

### 2.1 Inspection Query
```sql
SELECT id, tenant_id, incident_id, target_id, adapter_id, fence, state, attempt_count, updated_at
FROM effect_receipt
WHERE claim_token IS NULL OR state IN ('SENT', 'UNKNOWN', 'PREPARED')
ORDER BY created_at ASC;
```

### 2.2 Reconciliation Protocol
When running `CrossStoreRecoveryCoordinator`:
1. **Orphaned `PREPARED` Rows:** Any legacy receipt in `PREPARED` older than 60 seconds with no active lease or permit is quarantined to `NEEDS_REVIEW`.
2. **Pending `SENT` / `UNKNOWN` Rows:**
   - The coordinator calls `adapter.query_effect(tenant_id, target_id, idempotency_key)`.
   - If the adapter returns `COMMITTED`: the receipt is advanced to `CONFIRMED` -> `VERIFIED`.
   - If the adapter is unregistered or returns `NOT_FOUND` / `RETENTION_EXPIRED`: the receipt is transitioned to `NEEDS_REVIEW` with detail `"Legacy effect receipt with unregistered adapter safely quarantined during recovery"`.
3. **Associated Incident Alignment:** The associated incident status is aligned to `NEEDS_REVIEW` using `IncidentRepository.reconcile_status_projection_forward()`, ensuring the incident cannot be accidentally re-dispatched.

---

## 3. Operator Resolution of Quarantined Receipts
Incident commanders can review and resolve quarantined receipts via the OpsBlaze P2 Approval Cockpit or by manual administrative SQL:
```sql
-- Mark as reviewed by human commander:
UPDATE effect_receipt
SET state = 'FAILED', last_error = 'Quarantined legacy effect confirmed terminal by operator'
WHERE id = 'RECEIPT_UUID' AND state = 'NEEDS_REVIEW';
```
