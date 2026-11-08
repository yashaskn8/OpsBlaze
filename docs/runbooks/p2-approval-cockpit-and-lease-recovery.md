# Runbook: P2 Approval Cockpit & Dispatch Lease Recovery

**Product:** OpsBlaze / TITAN-20  
**Feature:** P2 — Version-Bound Approval Cockpit  
**Target Roles:** Incident Commander, Platform Engineer, SRE Lead, Operator, On-Call Responder  

---

## 1. Overview & Invariants

Feature P2 ensures that no consequential mutation is dispatched without explicit, human role-gated approval tied to exact target resource versions and plan content digests.

### Key Invariants
1. **Server-Side Role Authorization:** Only allowed human roles (`incident_commander`, `platform_engineer`, `sre_lead`, `operator`, `oncall_engineer`) may approve, reject, or revoke plans. Autonomous agents and LLMs are strictly forbidden from self-approval (HTTP 403 Forbidden).
2. **Version Fencing & Context Integrity:** The approval request binds `target_resource_version`, `status_version_at_creation`, `plan_digest`, and `action_digest`. If the target resource version drifts or if the incident context advances before approval/dispatch, the approval is rejected or revoked immediately.
3. **Two-Phase Dispatch Lease:** State transitions follow:
   $$\text{PENDING} \longrightarrow \text{APPROVED} \longrightarrow \text{DISPATCHING} \longrightarrow \text{CONSUMED}$$
   A worker claiming an approved plan is granted a bounded lease (`lease_id`, default TTL 60s). If the worker crashes or times out while dispatching, the lease expires. A subsequent retry worker may reclaim the lease once `dispatch_lease_expires_at <= now()`.
4. **Zombie Worker Rejection:** A worker attempting to finalize an action with an expired lease is rejected with `DispatchLeaseExpiredError` (HTTP 409 Conflict), preventing duplicate mutations.
5. **Approval Expiration:** Pending or approved requests that exceed `expires_at` (default TTL 300s) transition to `EXPIRED` automatically upon inspection.

---

## 2. Standard Operating Procedures

### 2.1 Inspecting Pending Approvals
Operators can review active approvals via the P2 Approval Cockpit UI or via API:
```bash
curl -s -H "X-Tenant-ID: tenant-prod" \
     -H "X-Actor-ID: user-123" \
     -H "X-Actor-Role: incident_commander" \
     http://localhost:8000/v1/approvals/appr-abc-123
```

Verify:
- `plan_digest`: Must match the approved plan manifest.
- `target_resource_version`: Must match the target's current live version.
- `state`: Must be `PENDING`.
- `expires_at`: Must be in the future.

### 2.2 Approving a Plan
```bash
curl -X POST http://localhost:8000/v1/approvals/appr-abc-123/approve \
     -H "Content-Type: application/json" \
     -H "X-Tenant-ID: tenant-prod" \
     -H "X-Actor-ID: ic-alice" \
     -H "X-Actor-Role: incident_commander" \
     -d '{
       "decision": "APPROVED",
       "reason": "Verified memory leak root cause and plan restart",
       "expected_resource_version": "v1"
     }'
```

### 2.3 Rejecting or Revoking an Approval
If an operator detects anomalous telemetry, wrong hypothesis, or desires manual intervention:
```bash
curl -X POST http://localhost:8000/v1/approvals/appr-abc-123/reject \
     -H "Content-Type: application/json" \
     -H "X-Tenant-ID: tenant-prod" \
     -H "X-Actor-ID: sre-bob" \
     -H "X-Actor-Role: sre_lead" \
     -d '{
       "decision": "REJECTED",
       "reason": "Target already restarted by autoscaler"
     }'
```

---

## 3. Failure & Recovery Modes

### 3.1 Dispatch Worker Crash During Execution (Lease Expiry)
**Symptom:** Approval remains in `DISPATCHING` state, but the worker process has died.  
**Resolution:**
1. A retry activity or replacement worker requests `claim_dispatch_lease()`.
2. The server inspects `dispatch_lease_expires_at`. If `expires_at <= now()`, the server issues a new `lease_id` and resets the expiry window.
3. If the worker needs immediate manual unsticking before TTL, an operator can revoke the request:
   ```bash
   curl -X POST http://localhost:8000/v1/approvals/appr-abc-123/revoke \
        -H "Content-Type: application/json" \
        -H "X-Tenant-ID: tenant-prod" \
        -H "X-Actor-ID: ic-alice" \
        -H "X-Actor-Role: incident_commander" \
        -d '{"decision": "REVOKED", "reason": "Operator manual override on crashed worker"}'
   ```

### 3.2 Target Version Drift
**Symptom:** Worker receives HTTP 409 Conflict with message `"Target resource version drift detected"`.  
**Root Cause:** An out-of-band deployment or concurrent process mutated the target between plan creation and dispatch.  
**Resolution:**
1. Approval is marked invalid.
2. Incident is returned to `INVESTIGATING` to execute a fresh probe against the new target version.
3. No mutation is executed against the stale target version.
