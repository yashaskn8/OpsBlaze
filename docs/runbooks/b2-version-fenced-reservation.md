# Operational Runbook: Feature B2 — Version-Fenced Resource Reservation & Contention Triage

*   **Component:** `backend/effects/fencing.py` (`ResourceReservationManager`, `MutationPermit`)
*   **Database Tables:** `resource_epoch`, `resource_reservation`
*   **Primary Metrics & Events:** `reservation.conflict`, `fence.stale_reject`, `target.version_conflict`, `lease.expired`

--------------------------------------------------------------------------------

## 1. Purpose

Feature B2 enforces at-most-one active mutation reservation per `(tenant_id, target_id)` and allocates strictly monotonic `BigInteger` fencing tokens (`fence = latest_fence + 1`) from `resource_epoch`. This runbook guides operators through diagnosing reservation contention, expired leases, stale fence rejections, and target version drift.

## 2. Inspecting Active & Historical Reservations in PostgreSQL

Run the following read-only queries against PostgreSQL (`opsblaze`):

```sql
-- 1. Inspect current fencing epoch and last verified version for a target
SELECT tenant_id, target_id, latest_fence, version_seen
FROM resource_epoch
WHERE target_id = '<target_id>';

-- 2. Inspect active (RESERVED or DISPATCHED) reservations
SELECT id, tenant_id, target_id, holder_incident_id, fence, state, expires_at, created_at
FROM resource_reservation
WHERE state IN ('RESERVED', 'DISPATCHED')
ORDER BY expires_at ASC;

-- 3. Check if an expired DISPATCHED reservation is blocked by an unresolved B3 receipt
SELECT r.id AS reservation_id, r.fence, r.state AS res_state, r.expires_at,
       e.id AS receipt_id, e.state AS effect_state, e.updated_at, e.last_error
FROM resource_reservation r
LEFT JOIN effect_receipt e
  ON e.tenant_id = r.tenant_id
 AND e.target_id = r.target_id
 AND e.fence = r.fence
WHERE r.state = 'DISPATCHED'
  AND r.expires_at <= now();
```

## 3. Triage Scenarios

### Scenario A: `reservation.conflict` (`ResourceAlreadyReservedError` / `ResourceUncertainEffectPendingError`)
*   **Symptom:** An incident attempting to enter `EXECUTING` fails to acquire a reservation on `target_id`.
*   **Root Causes:**
    1. Another incident for the same tenant currently holds a non-expired `RESERVED` or `DISPATCHED` reservation on `target_id`.
    2. A prior reservation in `DISPATCHED` state has an expired lease (`expires_at <= now()`), **but** its B3 `effect_receipt` is still in `SENT`, `UNKNOWN`, or `CONFIRMED`. By design (`ADR-0003` `[CRITICAL-1]`), B2 blocks competing reservations until B3 reconciles the ambiguous receipt to a terminal state (`VERIFIED`, `FAILED`, or `NEEDS_REVIEW`).
*   **Action:**
    *   If held by an active incident, wait for that incident's execution to finish and release the reservation.
    *   If blocked by an `UNKNOWN` / `SENT` receipt, follow `docs/runbooks/b3-unknown-effect-reconciliation.md` to complete B3 reconciliation first. **Never** manually delete rows from `resource_reservation` or reset `resource_epoch.latest_fence`.

### Scenario B: `fence.stale_reject` (`StaleFenceError` / `ReservationExpiredError`)
*   **Symptom:** A delayed worker wakes up after a GC pause or network stall and its dispatch attempt is rejected at either the PostgreSQL pre-dispatch gate or the target adapter (`adapter.stale_fence_violation`).
*   **Verification:** Confirm that `resource_epoch.latest_fence > rejected_fence` and that the old reservation row transitioned to `EXPIRED`.
*   **Action:** No manual database intervention is required. The stale worker was safely fenced off before mutating the target. If lease expirations occur frequently under normal latency, review the `lease_seconds` parameter passed to `acquire_reservation`.

### Scenario C: `target.version_conflict` (`TargetVersionConflictError`)
*   **Symptom:** The live target resource version returned by `adapter.read_resource()` does not match the approved plan's `expected_version` (out-of-band change or ABA drift).
*   **Action:**
    *   Inspect `resource_epoch.version_seen` (automatically updated to the live observed version when the conflict is detected).
    *   Re-run read-only investigation on the updated target version and obtain a fresh version-bound approval (`ApprovedPlanContext`) targeting the new `expected_version`.
