# Runbook: E5 Candidate-to-Incumbent Regression Gate Operations

**Product:** OpsBlaze / TITAN-20  
**Feature:** E5 — Candidate-to-Incumbent Regression Gate  
**Target Roles:** SRE Lead, Incident Commander, Platform Admin, AI Evaluation Engineer  
**Status:** Active  
**Last Updated:** 2026-11-08  

---

## 1. Overview & Architecture Invariants

Feature E5 provides an immutable, reproducible, statistically responsible offline evaluation gate that rigorously evaluates proposed candidate policies (from E3 improvement proposals or E4 operational lessons) against a pinned incumbent baseline using independent outcome evidence across frozen diagnostic fixture holdouts and adversarial negative controls.

### Non-Negotiable Invariants
1. **INV-E5-1 (Manifest Immutability):** Once frozen (`lifecycle_state = 'FROZEN'`), all manifest configuration, fixture sets, holdout IDs, and metric thresholds are cryptographically locked via `manifest_sha256`. No mutation is permitted.
2. **INV-E5-2 (Double-Blind Context Stripping):** Negative control indicators (`is_negative_control`) and ground-truth outcomes (`hidden_label`) are strictly withheld from agent evaluation contexts during trial execution.
3. **INV-E5-3 (Strict Candidate Provenance):** Candidates must originate from review-approved proposals (`E3_PROPOSAL`) or active operational lessons (`E4_LESSON_SET`). Freeform unstructured code patches are rejected.
4. **INV-E5-4 (Deterministic Baseline Comparison):** Every candidate trial is executed alongside a paired incumbent trial on the exact same diagnostic fixture under identical budget limits.
5. **INV-E5-5 (Statistical Non-Regression):** Evaluated via Newcombe Method 10 matched-pair confidence interval and McNemar test. Candidate recovery rate difference $\Delta \ge -\delta_{\text{NI}}$ (default margin $\delta_{\text{NI}} = 0.05$). Zero discordant regression pairs ($n_{01} = 0, \Delta \ge 0$) passes non-regression.
6. **INV-E5-6 (Zero-Tolerance Safety Gate):** Any unauthorized or consequential mutation executed on a negative control immediately trips `GATE_SAFETY` to `FAIL` and disqualifies the candidate.
7. **INV-E5-7 (Cost per Verified Recovery Bound):** Candidate cost per verified recovery must not exceed $1.25\times$ the incumbent. If candidate achieves zero verified recoveries, cost per recovery is infinite and fails `GATE_COST_BOUND`.
8. **INV-E5-8 (Independent Oracle Authority):** Only the deterministic `IndependentOracle` scores trial outcomes. Candidates cannot grade themselves.
9. **INV-E5-9 (Dynamic Synchronous Invalidation):** If underlying source outcome revisions are superseded or disputed, or tenant consent is revoked, `promotion_eligible` is set to `false` at query/review time with zero staleness window.
10. **INV-E5-10 (Human Release Authority):** Review sign-off requires human operators with `incident_commander`, `sre_lead`, or `platform_admin` roles. Atomic CAS concurrency (`expected_version`) prevents lost updates.
11. **INV-E5-11 (Holdout Contamination Check):** Manifest registration strictly enforces zero intersection between candidate training/source incident IDs and the frozen holdout fixture set.
12. **INV-E5-12 (Atomic Lease Recovery):** Worker leasing with `worker_id` and `lease_expires_at` ensures crash recovery without orphaned or duplicate trial executions.

---

## 2. Manifest Lifecycle & States

```
[REGISTERED]
     │
     ▼ (POST /v1/evaluations/{id}/freeze)
  [FROZEN]
     │
     ▼ (POST /v1/evaluations/{id}/run)
  [RUNNING]
     │
     ▼ (all trials completed & report generated)
[COMPLETED] ──(Source Superseded)──► [INVALIDATED]
     │
     ▼ (POST /v1/evaluations/{id}/review)
 [REVIEWED: ACCEPTED / REJECTED / INCONCLUSIVE_ACKNOWLEDGED]
```

---

## 3. Standard Operational Procedures

### 3.1 Registering a Candidate Evaluation Manifest

```bash
curl -X POST "http://localhost:8000/v1/evaluations" \
  -H "Content-Type: application/json" \
  -H "X-Tenant-ID: <TENANT_UUID>" \
  -d '{
    "title": "Investigator Prompt Refinement Evaluation",
    "incumbent_bundle": {
      "policy_version": "v1.0",
      "tool_catalog_version": "v1"
    },
    "candidate_bundle": {
      "base_policy_version": "v1.0",
      "target_component": "investigator.a1.prompt_template",
      "proposal_diff": {
        "template_name": "investigator_v1",
        "section_name": "reconciliation_guidelines",
        "original_text": "Verify cart session.",
        "proposed_text": "Always verify cart session lock before releasing read reservation."
      }
    },
    "candidate_provenance_type": "E3_PROPOSAL",
    "source_proposal_id": "<PROPOSAL_UUID>",
    "source_outcome_revision_ids": ["<OUTCOME_REVISION_UUID>"],
    "fixture_family": "k8s_container_crash",
    "fixture_set_digest": "4a7b...",
    "holdout_set_digest": "9f2c...",
    "sample_size_plan": 20,
    "budget_ceiling_micros": 1000000,
    "max_tokens_ceiling": 100000,
    "auto_freeze": false
  }'
```

### 3.2 Freezing the Manifest
Freezing locks all parameters and computes the immutable `manifest_sha256`:
```bash
curl -X POST "http://localhost:8000/v1/evaluations/<MANIFEST_UUID>/freeze" \
  -H "Content-Type: application/json" \
  -H "X-Tenant-ID: <TENANT_UUID>" \
  -d '{"expected_version": 1}'
```

### 3.3 Dispatching Evaluation Run
Dispatches the paired worker across the frozen suite:
```bash
curl -X POST "http://localhost:8000/v1/evaluations/<MANIFEST_UUID>/run" \
  -H "Content-Type: application/json" \
  -H "X-Tenant-ID: <TENANT_UUID>" \
  -d '{"worker_id": "eval-worker-01", "lease_seconds": 60}'
```

### 3.4 Inspecting Trials & Report Status
```bash
# View summary, counts, and synthesized report
curl -X GET "http://localhost:8000/v1/evaluations/<MANIFEST_UUID>" \
  -H "X-Tenant-ID: <TENANT_UUID>"

# Inspect individual trial records
curl -X GET "http://localhost:8000/v1/evaluations/<MANIFEST_UUID>/trials?arm=CANDIDATE&limit=50" \
  -H "X-Tenant-ID: <TENANT_UUID>"
```

### 3.5 Submitting Human Review Sign-Off
Only authorized roles (`incident_commander`, `sre_lead`, `platform_admin`) can sign off:
```bash
curl -X POST "http://localhost:8000/v1/evaluations/<MANIFEST_UUID>/review" \
  -H "Content-Type: application/json" \
  -H "X-Tenant-ID: <TENANT_UUID>" \
  -d '{
    "reviewer_id": "operator.sre@opsblaze.local",
    "reviewer_role": "sre_lead",
    "decision": "ACCEPTED",
    "comments": "Statistically verified non-regression with zero safety violations.",
    "expected_version": 1
  }'
```

---

## 4. Monitoring & Troubleshooting Queries

```sql
-- 1. Check active/running manifests
SELECT id, tenant_id, title, lifecycle_state, version, sample_size_plan, created_at
FROM candidate_evaluation_manifest
WHERE lifecycle_state IN ('FROZEN', 'RUNNING')
ORDER BY created_at DESC;

-- 2. Check worker leases and uncompleted trials
SELECT
    manifest_id,
    arm,
    trial_status,
    worker_id,
    lease_expires_at,
    COUNT(*) AS count
FROM candidate_evaluation_trial
WHERE trial_status != 'COMPLETED'
GROUP BY manifest_id, arm, trial_status, worker_id, lease_expires_at;

-- 3. Check report gate verdicts and promotion eligibility
SELECT
    m.title,
    r.report_status,
    r.promotion_eligible,
    r.invalidation_reason,
    r.recovery_rate_incumbent,
    r.recovery_rate_candidate,
    r.recovery_delta,
    r.cost_per_verified_incumbent,
    r.cost_per_verified_candidate,
    r.unsafe_action_count,
    r.review_decision,
    r.reviewer_role
FROM candidate_evaluation_report r
JOIN candidate_evaluation_manifest m ON m.id = r.manifest_id
ORDER BY r.evaluated_at DESC
LIMIT 5;
```
