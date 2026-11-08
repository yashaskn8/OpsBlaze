# Operational Runbook: Feature B3 — `UNKNOWN` Effect Triage & Reconciliation

*   **Component:** `backend/effects/receipts.py` (`ActionGateway`, `EffectReconciler`, `IndependentPostconditionVerifier`)
*   **Database Table:** `effect_receipt`
*   **Primary Metrics & Events:** `effect.unknown_age`, `reconciliation.attempt`, `effect.duplicate_reject`, `verifier.false_success`

--------------------------------------------------------------------------------

## 1. Purpose

Feature B3 guarantees that every consequential external mutation is durably recorded in `effect_receipt` (`PREPARED -> SENT`) before calling the external adapter. When a network timeout, lost response, or worker crash leaves the external outcome ambiguous, B3 transitions the receipt to `UNKNOWN` and prohibits blind retries. This runbook covers inspecting and resolving `UNKNOWN` and `NEEDS_REVIEW` receipts.

## 2. Inspecting Pending & Ambiguous Receipts in PostgreSQL

```sql
-- 1. List all pending UNKNOWN and SENT receipts ordered by age
SELECT id, tenant_id, incident_id, target_id, adapter_id, idempotency_key,
       fence, expected_version, observed_version, state, attempt_count,
       lease_owner, lease_expires_at, last_error,
        EXTRACT(EPOCH FROM (now() - created_at)) AS age_seconds
FROM effect_receipt
WHERE state IN ('UNKNOWN', 'SENT')
ORDER BY updated_at ASC;

-- 2. Inspect receipts escalated to NEEDS_REVIEW
SELECT id, tenant_id, incident_id, target_id, adapter_id, idempotency_key,
       fence, expected_version, observed_version, external_ref, last_error, updated_at
FROM effect_receipt
WHERE state = 'NEEDS_REVIEW'
ORDER BY updated_at DESC;
```

## 3. Triage & Resolution Procedures

### Scenario A: Receipt in `UNKNOWN` (or Expired `SENT`)
*   **Automated Path:** `EffectReconciler.reconcile_pending()` claims eligible rows via `FOR UPDATE SKIP LOCKED`, transitions expired `SENT` rows to `UNKNOWN`, and invokes `adapter.query_effect(tenant_id, target_id, idempotency_key)` outside the DB transaction:
    *   If `COMMITTED`: Transitions `UNKNOWN -> CONFIRMED`, runs `IndependentPostconditionVerifier` (`adapter.read_resource`), transitions `CONFIRMED -> VERIFIED`, and releases the B2 reservation.
    *   If `NOT_FOUND` (within the adapter's active `retention_horizon_seconds` and within `max_apply_attempts`): Performs a single controlled retry if the B2 reservation and expected version are still valid; otherwise transitions to `FAILED`.
*   **Operator Action if Stuck in `UNKNOWN`:**
    1. Check `last_error` on the `effect_receipt` row for `AdapterUnavailableError` or `AdapterLookupError` (status-store outage).
    2. Once adapter connectivity is restored, trigger `EffectReconciler.reconcile_pending()` (or let the next scheduled pass run).
    3. If `attempt_count > max_reconcile_attempts`, the reconciler automatically transitions the receipt to `NEEDS_REVIEW` and releases the B2 reservation.

### Scenario B: Receipt Escalated to `NEEDS_REVIEW` Due to Retention Gap (`adapter.retention_gap`)
*   **Symptom:** `effect.unknown_age` exceeded `adapter_conformance.retention_horizon_seconds` before `query_effect` could confirm whether the mutation committed.
*   **Why Automatic Retry Is Blocked:** Once the target's idempotency retention window has expired, re-sending `apply` could execute the mutation a second time.
*   **Action:**
    1. Call `adapter.read_resource(tenant_id, target_id)` (read-only inspection) to inspect `current_version`, `highest_fence_seen`, and `last_effect_id`.
    2. Compare `current_version` against the receipt's `expected_version`.

### Scenario C: Receipt Escalated to `NEEDS_REVIEW` Due to False Success (`verifier.false_success`)
*   **Symptom:** The adapter returned `APPLIED` or `COMMITTED`, but `IndependentPostconditionVerifier` (`read_resource`) found that `current_version != receipt.observed_version`, `last_effect_id != receipt.idempotency_key`, or `healthy is False`.
*   **Automatic Consequence:** B3 automatically quarantined the adapter (`status = 'QUARANTINED'` in `adapter_conformance`) and emitted `verifier.false_success` + `adapter.lookup_mismatch`.
*   **Action:** Follow `docs/runbooks/e8-adapter-quarantine-and-retest.md` to investigate the adapter defect before re-enabling it.
