# Runbook: E5 Invalid Trial Remediation & Report Revocation

**Product:** OpsBlaze / TITAN-20  
**Feature:** E5 — Candidate-to-Incumbent Regression Gate  
**Target Roles:** Platform Engineer, SRE On-Call, AI Incident Commander  
**Status:** Active  
**Last Updated:** 2026-11-08  

---

## 1. Context & Revocation Triggers

Under Feature E5, evaluation reports and candidate promotion eligibility are strictly conditional on:
1. **Source Integrity:** The underlying outcome revisions (`OutcomeRevision`) that provided the evidence for the candidate change must remain undisputed and un-superseded.
2. **Consent Validity:** The tenant's consent epoch (`ConsentEpoch.reuse_allowed`) must remain active.
3. **Execution Validity:** The proportion of invalid trials (timeouts, fixture corruption) must not exceed `max_invalid_pairs_ratio` (5%).
4. **Safety Integrity:** Zero unauthorized mutations on negative control fixtures.

When any of these conditions are violated, OpsBlaze enforces immediate, synchronous query-time dynamic invalidation (`INV-E5-9`).

---

## 2. Invalid Trial Remediation

### 2.1 Identifying Invalid Trials
Trials marked with `trial_status = 'INVALID'` or `validity_state != 'VALID'` are excluded from matched-pair analysis and increment `invalid_pairs_count`.

```sql
SELECT
    id,
    manifest_id,
    arm,
    fixture_id,
    validity_state,
    validity_reason,
    duration_ms
FROM candidate_evaluation_trial
WHERE validity_state != 'VALID'
ORDER BY created_at DESC;
```

### 2.2 Common Causes & Remediation
- **INVALID_FIXTURE_CORRUPT:** The fixture referenced in the manifest does not exist in the active fixture catalog or had its SHA-256 corrupted.
  - *Remediation:* Verify that the fixture digest passed at manifest creation matches the catalog version. Cancel the manifest (`POST /v1/evaluations/{id}/cancel`) and register a new manifest with corrected fixture digests.
- **INVALID_TIMEOUT:** The trial duration exceeded the tool/worker execution deadline.
  - *Remediation:* Check network/simulator latency for the target component. If `invalid_pairs_count / sample_size_plan > 0.05`, `GATE_COMPLETION` will trip to `FAIL`.
- **INVALID_ORACLE_OUTAGE:** The independent oracle service was unreachable during ground-truth adjudication.
  - *Remediation:* Inspect PostgreSQL connection pool and oracle dependency health.

---

## 3. Worker Lease Stalls & Crash Recovery

If an evaluation worker dies abruptly during execution:
1. **Detection:** The worker will stop heartbeating. The trial rows remain in `RUNNING` status with `lease_expires_at < NOW()`.
2. **Automatic Reclamation:** The runner query uses:
   ```sql
   WHERE trial_status = 'PENDING'
      OR (trial_status = 'RUNNING' AND lease_expires_at < NOW())
   FOR UPDATE SKIP LOCKED;
   ```
   Any surviving worker will automatically reclaim the expired trials without manual database intervention.
3. **Manual Lease Forfeiture (Emergency):**
   If a worker process is hung and locking trials without heartbeating:
   ```sql
   UPDATE candidate_evaluation_trial
   SET trial_status = 'PENDING',
       worker_id = NULL,
       lease_expires_at = NULL,
       updated_at = NOW()
   WHERE manifest_id = '<MANIFEST_UUID>'
     AND trial_status = 'RUNNING'
     AND lease_expires_at < NOW();
   ```

---

## 4. Source Supersession & Invalidation Handling

### 4.1 When an Outcome Revision is Disputed or Superseded
If an incident commander disputes an outcome revision (e.g., from `VERIFIED_SUCCESS` to `DISPUTED`), or files revision $N+1$:
- OpsBlaze does not require asynchronous batch re-auditing.
- The next time `GET /v1/evaluations/{id}` or `POST /v1/evaluations/{id}/review` is invoked, `CandidateReportService.check_source_eligibility` detects that:
  - `rev.dispute_reason IS NOT NULL`, OR
  - A newer revision exists: `revision_no > rev.revision_no`.
- The service immediately executes:
  ```python
  report.promotion_eligible = False
  report.invalidation_reason = f"Source outcome revision {rid} was superseded by revision {newer_rev}"
  ```
- Any pending or subsequent review submission returns `HTTP 409 Conflict` (`SourceEligibilityInvalidatedError`).

### 4.2 When Tenant Revokes Consent
If a customer toggles off data reuse:
```sql
UPDATE consent_epoch
SET reuse_allowed = false, updated_at = NOW()
WHERE tenant_id = '<TENANT_UUID>';
```
- All reports for this tenant immediately reflect `promotion_eligible = False` on read.
- Reason: `"Tenant consent epoch revoked: reuse_allowed is False"`.

---

## 5. Cancelling an In-Flight or Compromised Evaluation

To abort an evaluation run before completion or review:
```bash
curl -X POST "http://localhost:8000/v1/evaluations/<MANIFEST_UUID>/cancel" \
  -H "Content-Type: application/json" \
  -H "X-Tenant-ID: <TENANT_UUID>" \
  -d '{"reason": "Source proposal was retracted by security audit."}'
```

This immediately sets `lifecycle_state = 'CANCELLED'`. Workers checking manifest state will abort further execution, and no final report will be eligible for human sign-off.
