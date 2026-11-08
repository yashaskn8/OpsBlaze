# Runbook: Validated Operational Lesson Registry & Synchronous Invalidation

**Feature:** E4 — Validated Operational Lesson Registry  
**Version:** 1.0.0  
**Date:** 2026-11-08  
**Scope:** Lesson drafting eligibility, human reviewer validation, PostgreSQL 17 full-text search retrieval, instant query-time invalidation, and no-memory fallback operations.

---

## 1. Architecture Overview & Core Invariants

The Validated Operational Lesson Registry serves as the curated operational-memory store for OpsBlaze. It converts verified incident outcomes into searchable, attributable, consent-scoped lessons for the A1 Adaptive Probe Planner.

Non-negotiable invariants:
1. **Attributable Provenance (`INV-E4-1`):** A lesson can only be derived from a settled, undisputed E2 outcome (`VERIFIED_SUCCESS` or `VERIFIED_FAILURE`). Drafts from pending, observed, or disputed outcomes are rejected.
2. **Consent & Epoch Boundary (`INV-E4-2`):** Lessons are tagged with the tenant's active `consent_epoch`. If the tenant revokes consent or advances the epoch, all prior lessons become instantly unretrievable.
3. **Dual-Key Human Activation (`INV-E4-3`):** Drafted lessons begin in `SOURCE_CHECKED`. They can only enter `ACTIVE` after validation by an authorized human reviewer (`incident_commander`, `sre_lead`, `platform_admin`). Models cannot activate lessons.
4. **Synchronous Query-Time Invalidation (`INV-E4-4`):** The retrieval path re-verifies source outcome freshness in real-time. If a newer outcome revision (e.g., operator dispute) exists for the incident, or if consent is revoked, the lesson is instantly excluded from search results. Zero staleness window.
5. **No Vector Database Bloat (`INV-E4-5`):** All search indexing is implemented using PostgreSQL 17 native `STORED` `tsvector` generated columns and composite `(tenant_id, search_vector)` GIN indexes. Zero pgvector or external vector DB dependencies.
6. **Indirect Prompt Injection Neutralization (`INV-E4-6`):** All lesson text is sanitized before storage and formatted into immutable XML blocks (`<operational_lesson ...>`) during context assembly. Conversation role markers (`Human:`, `Assistant:`, `System:`) are stripped.
7. **Fail-Closed Fallback (`INV-E4-7`):** If lesson retrieval fails, times out, or encounters errors, the A1 planner fails closed and falls back to baseline no-memory mode. Unverified lessons are never injected into prompts.

---

## 2. Standard Operating Procedures

### 2.1 Drafting an Operational Lesson
```http
POST /v1/lessons/draft
X-Tenant-ID: <tenant-uuid>
Content-Type: application/json

{
  "title": "Payment Gateway Connection Pool Scaling Procedure",
  "summary": "During traffic spikes exceeding 5000 rps, always scale worker pool before raising gateway concurrency limit to prevent connection starvation.",
  "source_outcome_revision_id": "<settled-outcome-revision-uuid>",
  "lesson_type": "VERIFIED_RECOVERY",
  "confidence_band": "HIGH",
  "applicability_predicate": {
    "target_subsystem": "payment-gateway",
    "failure_codes": ["POOL_EXHAUSTION", "TIMEOUT"],
    "environment": "production"
  },
  "valid_days": 30
}
```

### 2.2 Validating and Activating a Lesson
An authorized human reviewer signs off on the lesson:
```http
POST /v1/lessons/{lesson_id}/validate
X-Tenant-ID: <tenant-uuid>
Content-Type: application/json

{
  "version": 1,
  "reviewer_id": "commander.bob@opsblaze.corp",
  "reviewer_role": "incident_commander"
}
```
**Effect:** State transitions to `ACTIVE`. The lesson is now indexed and searchable via full-text search.

### 2.3 Querying Operational Lessons (Investigator Integration)
The A1 planner queries lessons matching the current incident symptoms:
```http
GET /v1/lessons/search?query=payment+gateway+connection+pool&limit=3
X-Tenant-ID: <tenant-uuid>
```
**Response Format:**
```json
{
  "lessons": [
    {
      "lesson_id": "77777777-7777-4777-8777-777777777777",
      "version": 1,
      "title": "Payment Gateway Connection Pool Scaling Procedure",
      "sanitized_summary": "During traffic spikes exceeding 5000 rps...",
      "confidence_band": "HIGH",
      "lesson_type": "VERIFIED_RECOVERY",
      "source_digest": "rev:11111111-1111-4111-8111-111111111111:v1",
      "retrieval_digest": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    }
  ]
}
```

### 2.4 Revoking an Outdated or Invalid Lesson
If operational practices change or a lesson is deemed problematic:
```http
POST /v1/lessons/{lesson_id}/revoke
X-Tenant-ID: <tenant-uuid>
Content-Type: application/json

{
  "version": 1,
  "reason": "Superseded by automated autoscaling policy v3.0.",
  "actor_id": "sre.lead@opsblaze.corp",
  "actor_role": "sre_lead"
}
```
**Effect:** State transitions immediately to `REVOKED`. The lesson is never returned in future retrieval queries.

---

## 3. Emergency & Invalidation Protocols

### 3.1 Instant Invalidation on Outcome Dispute
When an operator marks a previously successful incident as `DISPUTED` (e.g., due to a relapse):
1. A new `OutcomeRevision` with `revision_no = 2` and `label = 'DISPUTED'` is inserted.
2. The `LessonRetriever` query contains:
   ```sql
   AND NOT EXISTS (
       SELECT 1 FROM outcome_revision r_newer
       WHERE r_newer.tenant_id = l.tenant_id
         AND r_newer.incident_id = r.incident_id
         AND r_newer.revision_no > r.revision_no
   )
   ```
3. Any lesson citing `revision_no = 1` is **immediately excluded** at query time. No asynchronous worker or cron sweep is needed.

### 3.2 Tenant Consent Revocation
If a tenant revokes experience data reuse (`reuse_allowed = False`) or increments their `current_epoch`:
1. All prior lessons are blocked at query time (`l.consent_epoch == e.current_epoch AND e.reuse_allowed = TRUE`).
2. The system seamlessly runs in **no-memory mode** without errors or degradation.
