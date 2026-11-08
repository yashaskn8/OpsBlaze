# Runbook: Failure Classification, Deterministic Clustering & Bounded Proposals

**Feature:** E3 — Failure Taxonomy & Improvement Proposal Queue  
**Version:** 1.0.0  
**Date:** 2026-11-08  
**Scope:** Automated first-stage failure classification, deterministic signature clustering, exemplar case maintenance, cluster quarantine, and guarded improvement proposal lifecycle.

---

## 1. Architecture Overview & Core Invariants

Feature E3 provides a governed diagnosis and proposal queue that converts independently adjudicated incident outcomes and execution evidence into structured failure cases and human-reviewable improvement proposals.

Non-negotiable invariants:
1. **Server Authority (`INV-E3-1`):** Autonomous models may suggest failure classifications and draft proposals, but deterministic backend validators own all schema validation, clustering signatures, and state machine transitions.
2. **Deterministic Clustering (`INV-E3-2`):** Cluster signatures are derived from an immutable 4-tuple: `(tenant_id, failure_origin_code, first_divergent_step_category, subsystem_version)`. No vector embeddings or non-deterministic clustering algorithms are permitted.
3. **Strict Bounded Proposals (`INV-E3-3`):** Proposals are strictly limited to bounded prompt refinements, context budget bounds, and tool description updates. Code modifications, database schema edits, SQL execution, credential tampering, and permission expansions are strictly forbidden (`extra="forbid"`).
4. **Human Review Gate (`INV-E3-4`):** An improvement proposal can NEVER transition to `EVALUATION_READY` without an authorized human reviewer (`incident_commander`, `sre_lead`, `platform_admin`) sign-off. Models cannot approve proposals.
5. **Synthetic Evaluation Incident Exclusion (`INV-E3-5`):** Synthetic evaluation incidents (e.g. S1 replay trials or benchmarks) are classified as `IGNORED` and strictly excluded from operational clusters and proposals to prevent self-referential training loops.
6. **CAS Optimistic Concurrency (`INV-E3-6`):** All proposal status transitions and review decisions require `expected_version`. Concurrent modifications fail with `409 Conflict`.

---

## 2. Standard Operating Procedures

### 2.1 Viewing and Triaging Failure Cases
Operators and SREs inspect failure cases to identify systemic issues across incident outcomes:
```http
GET /v1/failure-cases?status=CLASSIFIED&limit=50
X-Tenant-ID: <tenant-uuid>
```

Responses contain:
- `failure_origin_code`: One of `VERIFIED_FAILURE`, `DISPUTED_OUTCOME`, `UNCERTAIN_EVIDENCE`, `TOOL_AUTHORIZATION_DENIAL`, `RESOURCE_FENCING_FAILURE`, etc.
- `first_divergent_step_category`: One of `PROBE_TIMEOUT`, `FENCE_REJECTED`, `READBACK_MISMATCH`, `AUTHORIZATION_DENIED`, etc.
- `cluster_id`: Associated signature cluster.

### 2.2 Quarantining Drifting Failure Clusters
If a failure cluster exhibits divergent or conflicting failure modes due to an environmental change or software upgrade, operators must quarantine the cluster to prevent automated proposal drafting:
```http
POST /v1/failure-clusters/{cluster_id}/quarantine
X-Tenant-ID: <tenant-uuid>
Content-Type: application/json

{
  "reason": "Signature drift detected following v2.4.0 database connection pool deployment."
}
```
**Effect:**
- Cluster status transitions to `QUARANTINED`.
- Any subsequent attempt to draft an improvement proposal targeting this cluster is rejected with `422 Unprocessable Entity` (`Cluster is quarantined`).

### 2.3 Drafting Improvement Proposals (Model or Engineer)
Proposals must target approved server components and adhere to typed Pydantic schemas:
```http
POST /v1/improvements/draft
X-Tenant-ID: <tenant-uuid>
Content-Type: application/json

{
  "cluster_id": "<cluster-uuid>",
  "proposal_type": "PROMPT_REFINEMENT",
  "base_policy_version": "v1.2",
  "target_component": "investigator.a1.prompt_template",
  "structured_diff": {
    "template_id": "investigator_prompt_v1",
    "target_section": "reconciliation_guidance",
    "proposed_content": "Always verify replica health status with a 10s wait before probing connection pool.",
    "variable_deltas": {}
  },
  "hypothesis": "Eliminates premature timeout during replica failover.",
  "expected_benefit": "Reduces readback mismatch false alarms by 20%.",
  "risk_assessment": "Increases probe latency by up to 10 seconds.",
  "evaluation_plan": "Run S1 decision replay against the last 50 historical database failover incidents.",
  "drafter_id": "agentic-proposal-bot-01",
  "drafter_kind": "MODEL"
}
```

### 2.4 Submitting Proposals for Human Review
```http
POST /v1/improvements/{proposal_id}/submit
X-Tenant-ID: <tenant-uuid>
Content-Type: application/json

{
  "expected_version": 1
}
```
**Effect:** State transitions from `DRAFT` to `PROPOSED`. `version` increments to 2.

### 2.5 Operator Review and Evaluation Gate
An authorized reviewer must review the proposal before offline evaluation (E5):
```http
POST /v1/improvements/{proposal_id}/review
X-Tenant-ID: <tenant-uuid>
Content-Type: application/json

{
  "expected_version": 2,
  "reviewer_id": "sre.jane.doe@opsblaze.corp",
  "reviewer_role": "sre_lead",
  "decision": "APPROVED",
  "comments": "Approved for offline S1 paired replay evaluation."
}
```
**Permitted Decisions:**
- `APPROVED` -> transitions status to `EVALUATION_READY`.
- `REJECTED` -> transitions status to `REJECTED`.
- `CHANGES_REQUESTED` -> demotes status back to `DRAFT` with version increment.

---

## 3. Emergency & Troubleshooting Procedures

### 3.1 Smuggled Code or SQL Detected in Proposal
If a malicious or misconfigured agent attempts to submit arbitrary code, SQL `DROP TABLE`, or unauthorized file paths:
1. Deterministic validation in `ProposalValidator` intercepts the regex pattern or extra fields and rejects the request with HTTP `422`.
2. Inspect server logs for `PROPOSAL_VALIDATION_FAILED` to identify the originating `drafter_id`.
3. If suspicious activity persists, quarantine the cluster immediately via `/v1/failure-clusters/{cluster_id}/quarantine`.

### 3.2 Resolving CAS Concurrency Conflicts (`409 Conflict`)
If multiple SREs review a proposal concurrently:
1. Refresh proposal state: `GET /v1/improvements/proposals/{proposal_id}`.
2. Inspect the latest `version`, `reviewer_id`, and `review_decision`.
3. Submit review with the current `expected_version` reflecting latest state.
