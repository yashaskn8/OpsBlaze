# Operational Runbook: Feature E8 — Adapter Quarantine, Digest Drift & Conformance Retest

*   **Component:** `backend/adapters/conformance.py` (`ConformanceRunner`, `MutationCapabilityGate`)
*   **Database Table:** `adapter_conformance`
*   **Primary Metrics & Events:** `adapter.conformance_pass`, `adapter.stale_fence_violation`, `adapter.duplicate_effect`, `adapter.lookup_mismatch`, `adapter.retention_gap`, `adapter.enablement_revoked`

--------------------------------------------------------------------------------

## 1. Purpose

Feature E8 ensures that no target adapter is permitted to execute external mutations unless its exact `(adapter_id, adapter_version, code_digest)` has passed all 12 conformance and chaos scenarios (`status = 'CONFORMANT'`) and its attestation has not expired. If an adapter fails any conformance check, exhibits a runtime false-success mismatch (`verifier.false_success`), or changes its version/code digest without re-certification, `MutationCapabilityGate` immediately revokes mutation capability in PostgreSQL.

## 2. Inspecting Adapter Conformance State in PostgreSQL

```sql
SELECT adapter_id, adapter_version, code_digest, status,
       retention_horizon_seconds, verified_at, expires_at, updated_at,
       conformance_evidence_json
FROM adapter_conformance
ORDER BY updated_at DESC;
```

## 3. Triage & Re-Certification Procedures

### Scenario A: Adapter Status is `QUARANTINED`
*   **Symptom:** All B2 reservations and B3 dispatches targeting `adapter_id` fail closed with `AdapterQuarantinedError`.
*   **Diagnosis:**
    1. Inspect `conformance_evidence_json->>'quarantine_reason'` and `conformance_evidence_json->'scenarios'` on the `adapter_conformance` row.
    2. Check recent `adapter.enablement_revoked` and `verifier.false_success` structured events.
*   **Remediation & Retest:**
    1. Fix the underlying adapter defect (e.g., stale fence handling, version CAS enforcement, or postcondition accuracy).
    2. Re-run the 12-scenario E8 conformance harness via `ConformanceRunner.run_conformance_suite(adapter)`.
    3. Verify that all 12 scenarios pass (`report.passed is True`), `status` transitions to `CONFORMANT`, and `adapter.conformance_pass` is emitted.

### Scenario B: Adapter Status is `REVOKED_DIGEST_MISMATCH`
*   **Symptom:** `MutationCapabilityGate.verify_mutation_allowed()` raises `AdapterDigestMismatchError` and emits `adapter.enablement_revoked` with `reason="version_or_code_digest_mismatch"`.
*   **Root Cause:** The adapter code or version was modified or upgraded (`compute_adapter_code_digest(adapter)` changed) without running the E8 conformance suite against the new build.
*   **Remediation:** Execute `ConformanceRunner.run_conformance_suite(adapter)` against the new adapter build so all 12 chaos scenarios are verified and the new `(adapter_version, code_digest)` is recorded in `adapter_conformance`.

### Scenario C: Adapter Status is `EXPIRED`
*   **Symptom:** `MutationCapabilityGate.verify_mutation_allowed()` raises `AdapterEnablementExpiredError` because `now >= expires_at`.
*   **Remediation:** Re-run `ConformanceRunner.run_conformance_suite(adapter, validity_seconds=86400)` to refresh the conformance attestation window.
